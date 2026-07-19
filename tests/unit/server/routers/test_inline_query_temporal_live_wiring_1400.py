"""Story #1400: POST /api/query's live wiring to the async-hybrid temporal path.

Mirrors test_search_code_temporal_live_wiring_1400.py for the REST door.
Scenario 12: an identical logical query must resolve via the SAME shared
execute_live_temporal_search entry point as the MCP door, each door
post-processing for its own protocol. Scenario 3: the REST handoff shape
is HTTP 202 (accepted, still processing) with job_id/partial_results/
continue_polling/error_code=TEMPORAL_QUERY_DEFERRED.

Only the DEEP dispatch call (execute_live_temporal_search) is mocked --
it already has its own dedicated real-BGM/real-PayloadCache test suite.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies
from code_indexer.server.routers.inline_query import register_query_routes


def _make_user() -> User:
    return User(
        username="alice",
        password_hash="irrelevant",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def app_and_client(tmp_path):
    app = FastAPI()
    mock_semantic_query_manager = MagicMock()
    mock_activated_repo_manager = MagicMock()
    repo_dir = tmp_path / "activated-repo"
    repo_dir.mkdir()
    mock_activated_repo_manager.activated_repos_dir = str(tmp_path)
    register_query_routes(
        app,
        semantic_query_manager=mock_semantic_query_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )

    app.dependency_overrides[dependencies.get_current_user] = _make_user

    app.state.background_job_manager = MagicMock()
    app.state.payload_cache = MagicMock()

    client = TestClient(app)
    return app, client


def _temporal_payload(**overrides):
    base = {
        "query_text": "auth logic",
        "repository_alias": "activated-repo",
        "time_range": "2024-01-01..2024-12-31",
        "limit": 10,
    }
    base.update(overrides)
    return base


class TestCompletedFastPathBody:
    def test_completed_result_maps_to_standard_json_body(self, app_and_client):
        _app, client = app_and_client
        fake_result = {
            "status": "completed",
            "job_id": "job-123",
            "results": [{"file_path": "a.py"}],
            "shards_completed": 1,
            "shards_total": 1,
            "unranked": True,
        }
        with patch(
            "code_indexer.server.routers.inline_query.execute_live_temporal_search",
            return_value=fake_result,
        ) as mock_dispatch:
            response = client.post("/api/query", json=_temporal_payload())

        assert response.status_code == 200
        body = response.json()
        assert body["results"] == [{"file_path": "a.py"}]
        assert body["total_results"] == 1
        assert "query_metadata" in body
        assert "job_id" not in body
        mock_dispatch.assert_called_once()

    def test_dispatch_called_with_correctly_populated_worker_input(
        self, app_and_client
    ):
        _app, client = app_and_client
        fake_result = {
            "status": "completed",
            "job_id": "job-123",
            "results": [],
            "shards_completed": 1,
            "shards_total": 1,
            "unranked": True,
        }
        with patch(
            "code_indexer.server.routers.inline_query.execute_live_temporal_search",
            return_value=fake_result,
        ) as mock_dispatch:
            client.post("/api/query", json=_temporal_payload())

        _call_args, call_kwargs = mock_dispatch.call_args
        worker_input = call_kwargs["worker_input"]
        assert worker_input.query_text == "auth logic"
        assert worker_input.repository_alias == "activated-repo"
        assert worker_input.username == "alice"


class TestHandoffBody:
    def test_waiting_result_maps_to_deferred_handoff_body_with_202(
        self, app_and_client
    ):
        _app, client = app_and_client
        fake_result = {
            "status": "waiting",
            "job_id": "job-456",
            "continue_polling": True,
            "partial_results": [],
            "shards_completed": 0,
            "shards_total": None,
            "unranked": True,
        }
        with patch(
            "code_indexer.server.routers.inline_query.execute_live_temporal_search",
            return_value=fake_result,
        ):
            response = client.post("/api/query", json=_temporal_payload())

        assert response.status_code == 202
        body = response.json()
        assert body["job_id"] == "job-456"
        assert body["continue_polling"] is True
        assert body["error_code"] == "TEMPORAL_QUERY_DEFERRED"
        assert body["partial_results"] == []


class TestSearchEventTelemetry:
    def test_completed_temporal_query_enqueues_accurate_result_count(
        self, app_and_client
    ):
        """Code review finding: the finally block's telemetry enqueue DOES
        fire for the temporal early-return path (Python finally semantics),
        but result_count was hardcoded to 0 regardless of the real result
        count -- silently wrong data, not a dead path. This proves the fix:
        result_count on the enqueued record matches the real result count."""
        _app, client = app_and_client
        mock_writer = MagicMock()
        _app.state.search_event_log_writer = mock_writer

        fake_result = {
            "status": "completed",
            "job_id": "job-123",
            "results": [{"file_path": "a.py"}, {"file_path": "b.py"}],
            "shards_completed": 1,
            "shards_total": 1,
            "unranked": True,
        }
        with patch(
            "code_indexer.server.routers.inline_query.execute_live_temporal_search",
            return_value=fake_result,
        ):
            response = client.post("/api/query", json=_temporal_payload())

        assert response.status_code == 200
        assert mock_writer.enqueue.called
        enqueued_record = mock_writer.enqueue.call_args[0][0]
        assert enqueued_record.result_count == 2


class TestHandlerDeadlineMonotonicWiring:
    """Issue #1435: REST must now thread a real, computed
    handler_deadline_monotonic through to execute_live_temporal_search,
    mirroring MCP's protocol.py _invoke_handler
    (time.monotonic() + timeout_seconds) instead of the previous hardcoded
    None -- giving REST the same outer safety-margin cap on the temporal
    inline wait that MCP already has."""

    def test_dispatch_called_with_non_none_handler_deadline_bracketed_by_configured_timeout(
        self, app_and_client, tmp_path
    ):
        import time

        from code_indexer.server.services.config_service import ConfigService

        _app, client = app_and_client
        real_config_service = ConfigService(server_dir_path=str(tmp_path / "cfgsvc"))
        real_config_service.update_setting(
            "search_timeouts", "rest_query_handler_timeout_seconds", 45
        )

        fake_result = {
            "status": "completed",
            "job_id": "job-789",
            "results": [],
            "shards_completed": 1,
            "shards_total": 1,
            "unranked": True,
        }
        with (
            patch(
                "code_indexer.server.services.config_service.get_config_service",
                return_value=real_config_service,
            ),
            patch(
                "code_indexer.server.routers.inline_query.execute_live_temporal_search",
                return_value=fake_result,
            ) as mock_dispatch,
        ):
            before = time.monotonic()
            client.post("/api/query", json=_temporal_payload())
            after = time.monotonic()

        _call_args, call_kwargs = mock_dispatch.call_args
        deadline = call_kwargs["handler_deadline_monotonic"]

        assert deadline is not None
        assert isinstance(deadline, float)
        assert before + 45 <= deadline <= after + 45


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
