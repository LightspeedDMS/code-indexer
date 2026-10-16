"""Bug #2109: temporal searches are recorded with search_type "temporal".

Each test drives a real front door (MCP search_code or REST POST /api/query)
and reads the row the real SearchEventLogWriter wrote to a REAL store
(SQLite and PostgreSQL). Only the deep temporal dispatch
(execute_live_temporal_search, which has its own real-BGM/real-PayloadCache
suite) is replaced by a double returning a known outcome -- the same seam the
existing Story #1400 wiring tests use.
"""

import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

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

_DISPATCH_TARGETS = (
    "code_indexer.server.mcp.handlers.search.temporal_search."
    "execute_live_temporal_search",
    "code_indexer.server.routers.inline_query.execute_live_temporal_search",
)

_RESULTS: List[Dict[str, Any]] = [
    {"file_path": "a.py", "score": 0.9},
    {"file_path": "b.py", "score": 0.8},
]
_COMPLETED = {"status": "completed", "job_id": "job-1", "results": _RESULTS}
_DEFERRED = {
    "status": "waiting",
    "job_id": "job-2",
    "partial_results": [],
    "shards_completed": 0,
    "shards_total": 4,
}


class _ActivatedRepos:
    """ActivatedRepoManager stand-in: every alias is activated under a dir."""

    def __init__(self, root: Path) -> None:
        self.activated_repos_dir = str(root)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return True

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        if alias == "missing-repo":
            raise ValueError(f"Repository '{alias}' not found")
        path = Path(self.activated_repos_dir) / username / alias
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class TemporalEnv(SimpleNamespace):
    rec: Recorder
    state: SimpleNamespace
    repos: _ActivatedRepos


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[TemporalEnv]:
    repos = _ActivatedRepos(tmp_path / "activated")
    with recorder(request.param, tmp_path) as rec:
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            payload_cache=SimpleNamespace(),
            background_job_manager=SimpleNamespace(),
            access_filtering_service=None,
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            activated_repo_manager=repos,
            golden_repo_manager=None,
            background_job_manager=state.background_job_manager,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            yield TemporalEnv(rec=rec, state=state, repos=repos)


def _dispatching(outcome: Dict[str, Any]) -> ExitStack:
    """Patch both doors' deep temporal dispatch to return `outcome`."""
    stack = ExitStack()
    for target in _DISPATCH_TARGETS:
        stack.enter_context(patch(target, return_value=outcome))
    return stack


def _mcp_temporal(username: str, **extra: Any) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search import search_code

    params = {
        "repository_alias": "example-repo",
        "query_text": "token refresh",
        "time_range": "2024-01-01..2024-12-31",
        "limit": 10,
        **extra,
    }
    response = search_code(params, make_user(username))
    return dict(json.loads(response["content"][0]["text"]))


def _rest_temporal(env: TemporalEnv, username: str, **extra: Any) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers.inline_query import register_query_routes

    app = FastAPI()
    register_query_routes(
        app, semantic_query_manager=None, activated_repo_manager=env.repos
    )
    for name, value in vars(env.state).items():
        setattr(app.state, name, value)
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user(
        username
    )
    payload = {
        "query_text": "token refresh",
        "repository_alias": "example-repo",
        "time_range": "2024-01-01..2024-12-31",
        "limit": 10,
        **extra,
    }
    return TestClient(app).post("/api/query", json=payload)


class TestMcpTemporal:
    def test_completed_temporal_search_records_temporal_row(self, env):
        with _dispatching(_COMPLETED):
            body = _mcp_temporal("alice", search_mode="hybrid")
        assert body["success"] is True, body

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["result_count"]) == (
            "temporal",
            "alice",
            2,
        )
        assert row["repo_alias"] == "example-repo"
        assert row["query_text"] == "token refresh"

    def test_deferred_temporal_search_records_no_row(self, env):
        with _dispatching(_DEFERRED):
            body = _mcp_temporal("alice")
        assert body["success"] is False
        assert body["job_id"] == "job-2"

        assert env.rec.rows() == []


class TestRestTemporal:
    def test_completed_temporal_search_records_temporal_row(self, env):
        with _dispatching(_COMPLETED):
            response = _rest_temporal(env, "bob", search_mode="semantic")
        assert response.status_code == 200, response.text

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["result_count"]) == (
            "temporal",
            "bob",
            2,
        )

    def test_deferred_temporal_search_records_no_row(self, env):
        with _dispatching(_DEFERRED):
            response = _rest_temporal(env, "bob")
        assert response.status_code == 202, response.text

        assert env.rec.rows() == []


class TestTemporalRejectionsRecordNothing:
    """Story #2117: a temporal request rejected before its repository
    resolved (alias refused, unknown repository, a required service not
    wired) writes no row at either door."""

    def test_mcp_list_alias(self, env):
        with _dispatching(_COMPLETED):
            body = _mcp_temporal("alice", repository_alias=["a-repo", "b-repo"])
        assert body["error_code"] == "TEMPORAL_SINGLE_REPO_REQUIRED", body
        assert env.rec.rows() == []

    def test_mcp_unknown_activated_repository(self, env):
        with _dispatching(_COMPLETED):
            body = _mcp_temporal("alice", repository_alias="missing-repo")
        assert body["success"] is False, body
        assert env.rec.rows() == []

    def test_mcp_background_services_missing(self, env):
        from code_indexer.server.mcp.handlers import _utils

        with patch.object(_utils.app_module, "background_job_manager", None):
            with _dispatching(_COMPLETED):
                body = _mcp_temporal("alice")
        assert body["success"] is False, body
        assert env.rec.rows() == []

    def test_rest_missing_alias(self, env):
        with _dispatching(_COMPLETED):
            response = _rest_temporal(env, "bob", repository_alias=None)
        assert response.status_code == 400, response.text
        assert env.rec.rows() == []

    def test_rest_unknown_global_alias(self, env, tmp_path):
        env.state.golden_repos_dir = str(tmp_path / "golden")
        with _dispatching(_COMPLETED):
            response = _rest_temporal(env, "bob", repository_alias="nope-global")
        assert response.status_code == 404, response.text
        assert env.rec.rows() == []

    def test_rest_golden_repos_dir_not_configured(self, env):
        with _dispatching(_COMPLETED):
            response = _rest_temporal(env, "bob", repository_alias="nope-global")
        assert response.status_code == 503, response.text
        assert env.rec.rows() == []


class TestTemporalFailures:
    def test_rest_dispatch_failure_records_its_error_code(self, env):
        failed = {
            "status": "failed",
            "job_id": "job-3",
            "error": "capacity exhausted",
            "error_code": "TEMPORAL_DEDUP_CAPACITY_EXHAUSTED",
        }
        with _dispatching(failed):
            response = _rest_temporal(env, "bob")
        assert response.status_code == 502, response.text

        [row] = env.rec.rows()
        assert (row["outcome"], row["error_class"], row["repo_alias"]) == (
            "failure",
            "temporal_dedup_capacity_exhausted",
            "example-repo",
        )
