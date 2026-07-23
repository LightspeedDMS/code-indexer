"""Story #1400: search_code's live wiring to the async-hybrid temporal path.

This is the piece the coordinator explicitly flagged as missing: search_code
must actually build TemporalWorkerInput, resolve repo_path, and call
execute_live_temporal_search -- replacing the old fully-synchronous
_execute_temporal_query call for the temporal branch. Without this wiring,
none of the async-hybrid machinery (worker/dedup/poll/handoff) is reachable
by a real client.

Only the DEEP dispatch call (execute_live_temporal_search) is mocked here --
it already has its own dedicated real-BGM/real-PayloadCache test suite
(test_temporal_live_dispatch_1400.py). This file tests the search_code
integration point itself: alias validation, repo_path resolution, and
envelope-shape mapping (Scenario 1 fast-path vs Scenario 2/3 handoff).
"""

from datetime import datetime
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"


def _make_user(username: str = "alice") -> User:
    return User(
        username=username,
        password_hash=_DUMMY_HASH,
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


def _base_params(**overrides: Any) -> Dict[str, Any]:
    base = {
        "repository_alias": "my-repo",
        "query_text": "auth logic",
        "time_range": "2024-01-01..2024-12-31",
        "limit": 10,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _patch_app_module(monkeypatch, tmp_path):
    """Minimal real-shaped app_module stand-in: activated_repo_manager
    resolves a real (but empty) directory as the repo path; no golden
    repo manager needed for the activated-repo case tested here."""
    import code_indexer.server.mcp.handlers._utils as utils_module

    repo_dir = tmp_path / "activated-repo"
    repo_dir.mkdir()

    mock_app_module = MagicMock()
    mock_app_module.activated_repo_manager.get_activated_repo_path.return_value = str(
        repo_dir
    )
    mock_app_module.activated_repo_manager.user_has_activated_repo.return_value = True
    mock_app_module.app.state.payload_cache = MagicMock()
    mock_app_module.background_job_manager = MagicMock()

    monkeypatch.setattr(utils_module, "app_module", mock_app_module)
    import code_indexer.server.mcp.handlers.search as search_module

    monkeypatch.setattr(search_module._utils, "app_module", mock_app_module)
    yield mock_app_module


class TestAliasValidationRejection:
    def test_missing_alias_returns_temporal_alias_required_error(self):
        from code_indexer.server.mcp.handlers.search import search_code

        params = _base_params(repository_alias=None)
        result = search_code(params, _make_user())

        import json

        body = json.loads(result["content"][0]["text"])
        assert body["success"] is False
        assert body.get("error_code") == "TEMPORAL_ALIAS_REQUIRED"

    def test_list_alias_returns_temporal_single_repo_required_error(self):
        from code_indexer.server.mcp.handlers.search import search_code

        params = _base_params(repository_alias=["repo-a", "repo-b"])
        result = search_code(params, _make_user())

        import json

        body = json.loads(result["content"][0]["text"])
        assert body["success"] is False
        assert body.get("error_code") == "TEMPORAL_SINGLE_REPO_REQUIRED"


class TestCompletedFastPathEnvelope:
    def test_completed_result_maps_to_standard_success_envelope(self):
        """Scenario 1: fast completion returns the unchanged success
        envelope -- no job_id/status/partial_results fields."""
        from code_indexer.server.mcp.handlers.search import search_code

        fake_result = {
            "status": "completed",
            "job_id": "job-123",
            "results": [{"file_path": "a.py"}],
            "shards_completed": 1,
            "shards_total": 1,
            "unranked": True,
        }
        with patch(
            "code_indexer.server.mcp.handlers.search.execute_live_temporal_search",
            return_value=fake_result,
        ) as mock_dispatch:
            result = search_code(_base_params(), _make_user())

        import json

        body = json.loads(result["content"][0]["text"])
        assert body["success"] is True
        assert "job_id" not in body
        assert "status" not in body
        assert "partial_results" not in body
        mock_dispatch.assert_called_once()


class TestHandoffEnvelope:
    def test_waiting_result_maps_to_deferred_failure_envelope(self):
        """Scenario 2/3: exceeding the inline wait window returns the
        standard FAILURE envelope (success=False) carrying the additive
        async-hybrid fields -- a dumb client sees only a plain timeout."""
        from code_indexer.server.mcp.handlers.search import search_code

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
            "code_indexer.server.mcp.handlers.search.execute_live_temporal_search",
            return_value=fake_result,
        ):
            result = search_code(_base_params(), _make_user())

        import json

        body = json.loads(result["content"][0]["text"])
        assert body["success"] is False
        assert body["job_id"] == "job-456"
        assert body["continue_polling"] is True
        assert body["error_code"] == "TEMPORAL_QUERY_DEFERRED"
        assert "error" in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
