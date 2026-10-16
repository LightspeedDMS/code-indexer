"""Bug #2110: the recorded search_type is the mode that ran, from one list.

- REST POST /api/query: a hybrid request that runs as semantic (no FTS index)
  is recorded as "semantic".
- MCP search_code: an unknown search_mode is rejected, never stored.
- The record builder refuses any search_type outside SEARCH_TYPES.
- The Query Analytics Export page's Search Type options come from the same
  SEARCH_TYPES list.

Rows are read back from a REAL store (SQLite and PostgreSQL) written by the
REAL SearchEventLogWriter. The provider-backed semantic search
(SemanticQueryManager) is a double returning known results, as in the
existing REST search-event tests.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    make_user,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]


def _semantic_results(n: int) -> Dict[str, Any]:
    return {
        "results": [
            {
                "file_path": f"src/module_{i}.py",
                "line_number": 1,
                "code_snippet": "def example(): pass",
                "similarity_score": 0.9,
                "repository_alias": "example-repo",
            }
            for i in range(n)
        ],
        "total_results": n,
        "query_metadata": {},
    }


class _SemanticSearch:
    """SemanticQueryManager double: returns `n` known results."""

    def __init__(self, n: int) -> None:
        self._n = n

    def query_user_repositories(self, **_: Any) -> Dict[str, Any]:
        return _semantic_results(self._n)


class _ActivatedRepos:
    """ActivatedRepoManager stand-in: one activated repo with no FTS index."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self.activated_repos_dir = str(repo.parent)

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [{"user_alias": "example-repo", "username": username}]

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return str(self._repo)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return True


class Env(SimpleNamespace):
    rec: Recorder
    repo: Path


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    repo = tmp_path / "repo"
    repo.mkdir()
    with recorder(request.param, tmp_path) as rec:
        yield Env(rec=rec, repo=repo)


def _rest_query(env: Env, payload: Dict[str, Any], writer: Any = None) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers.inline_query import register_query_routes

    app = FastAPI()
    register_query_routes(
        app,
        semantic_query_manager=_SemanticSearch(2),
        activated_repo_manager=_ActivatedRepos(env.repo),
    )
    app.state.search_event_log_writer = writer or env.rec.writer
    app.state.payload_cache = None
    app.state.access_filtering_service = None
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user("alice")
    return TestClient(app).post("/api/query", json=payload)


class TestRestRecordsThroughSharedBuilder:
    """REST /api/query records through the shared record builder."""

    def test_recording_failure_is_reported_at_warning_with_its_type(self, env, caplog):
        import logging

        def _broken_enqueue(record: Any) -> None:
            raise RuntimeError("writer unavailable")

        caplog.set_level(logging.WARNING)
        response = _rest_query(
            env,
            {"query_text": "token refresh", "repository_alias": "example-repo"},
            writer=SimpleNamespace(enqueue=_broken_enqueue),
        )

        assert response.status_code == 200, response.text
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and r.name == "code_indexer.server.services.search_event_recording"
        ]
        assert len(warnings) == 1, warnings
        assert "RuntimeError" in warnings[0]
        assert "semantic" in warnings[0]


class TestRestRecordsExecutedMode:
    def test_hybrid_without_fts_index_is_recorded_as_semantic(self, env):
        response = _rest_query(
            env,
            {
                "query_text": "token refresh",
                "repository_alias": "example-repo",
                "search_mode": "hybrid",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["search_mode"] == "semantic"

        [row] = env.rec.rows()
        assert (row["search_type"], row["result_count"]) == ("semantic", 2)


class TestMcpRejectsUnknownMode:
    def test_unknown_search_mode_is_rejected_and_not_stored(self, env):
        from code_indexer.server.mcp.handlers.search import search_code

        app_module = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(search_event_log_writer=env.rec.writer)
            ),
            activated_repo_manager=None,
            golden_repo_manager=None,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            response = search_code(
                {
                    "repository_alias": "example-repo",
                    "query_text": "token refresh",
                    "search_mode": "semantik",
                },
                make_user("alice"),
            )
        body = json.loads(response["content"][0]["text"])

        assert body["success"] is False
        assert "search_mode" in body["error"]
        assert env.rec.rows() == []


class TestRecordBuilderRefusesUnknownType:
    def test_unknown_search_type_is_not_stored(self, env, caplog):
        from code_indexer.server.services.search_event_recording import (
            SearchOrigin,
            record_search_event,
        )

        record_search_event(
            env.rec.writer,
            origin=SearchOrigin(tool="search_code", door="mcp"),
            username="alice",
            repo_alias="example-repo",
            search_type="semantik",
            query_text="token refresh",
            result_count=1,
            total_latency_ms=5,
            correlation_id=None,
        )

        assert env.rec.rows() == []
        assert "semantik" in caplog.text


class TestExportPageUsesSharedList:
    def test_search_type_options_come_from_search_types(self, monkeypatch):
        from fastapi.testclient import TestClient

        from code_indexer.server.app import app
        from code_indexer.server.services import search_event_recording

        monkeypatch.setattr(
            search_event_recording,
            "SEARCH_TYPES",
            search_event_recording.SEARCH_TYPES + ("sentinel-type",),
        )
        session = MagicMock(role="admin", username="admin", csrf_token="t")
        session_manager = MagicMock()
        session_manager.get_session.return_value = session
        session_manager._serializer.secret_key = "unit-test-secret"
        session_manager._serializer.secret_keys = ["unit-test-secret"]
        with patch(
            "code_indexer.server.web.routes.get_session_manager",
            return_value=session_manager,
        ):
            html = TestClient(app).get("/admin/analytics-export").text

        for search_type in search_event_recording.SEARCH_TYPES:
            assert f'<option value="{search_type}">' in html, search_type
