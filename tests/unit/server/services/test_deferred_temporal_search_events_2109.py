"""Bug #2109 / #2113: a deferred temporal search is recorded when its result
is delivered.

A temporal search that outlives the inline wait hands off to a background
job (MCP TEMPORAL_QUERY_DEFERRED / REST 202) and records nothing then. The
handoff leaves a pending record in the cluster-shared PayloadCache, keyed by
(job_id, caller); the result door (MCP poll_search_job, REST GET
/api/query/result/{job_id}) that first delivers the COMPLETED result to that
caller claims it and records one "temporal" row with the real result count.

Every test uses a REAL PayloadCache backend and a REAL search_event_log store
of the same kind (SQLite and PostgreSQL) through the REAL writer. The deep
temporal dispatch (execute_live_temporal_search) is replaced by a double
returning a deferred handoff, as in the other #2109 tests; the job's
completion is a REAL terminal temporal snapshot written with
store_temporal_snapshot, and the job status comes from a small
BackgroundJobManager stand-in.
"""

import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    make_user,
    payload_cache_of,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_JOB_ID = "job-deferred-1"
_DISPATCH_TARGETS = (
    "code_indexer.server.mcp.handlers.search.temporal_search."
    "execute_live_temporal_search",
    "code_indexer.server.routers.inline_query.execute_live_temporal_search",
)
_DEFERRED = {
    "status": "waiting",
    "job_id": _JOB_ID,
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
        path = Path(self.activated_repos_dir) / username / alias
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


class _Jobs:
    """BackgroundJobManager stand-in: get_job_status answers a job's status
    to the users who submitted it (None to anyone else, as the real one)."""

    def __init__(self) -> None:
        self.status: Dict[str, Any] = {"status": "running"}
        self.owners: List[str] = []

    def get_job_status(
        self, job_id: str, username: str, is_admin: bool = False
    ) -> Optional[Dict[str, Any]]:
        if job_id != _JOB_ID or username not in self.owners:
            return None
        return {**self.status, "operation_type": "temporal_query"}


class _AllowAll:
    """AccessFilteringService stand-in: a normal user who may see every
    result."""

    def is_admin_user(self, username: str) -> bool:
        return False

    def calculate_over_fetch_limit(self, limit: int) -> int:
        return limit

    def filter_query_results(self, results: Any, username: str) -> Any:
        return results


class Env(SimpleNamespace):
    rec: Recorder
    cache: Any
    jobs: _Jobs
    state: SimpleNamespace
    repos: _ActivatedRepos


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    repos = _ActivatedRepos(tmp_path / "activated")
    jobs = _Jobs()
    with ExitStack() as stack:
        rec = stack.enter_context(recorder(request.param, tmp_path))
        cache = stack.enter_context(payload_cache_of(request.param, tmp_path))
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            payload_cache=cache,
            background_job_manager=jobs,
            access_filtering_service=_AllowAll(),
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            activated_repo_manager=repos,
            golden_repo_manager=None,
            background_job_manager=jobs,
        )
        stack.enter_context(
            patch("code_indexer.server.mcp.handlers._utils.app_module", app_module)
        )
        for target in _DISPATCH_TARGETS:
            stack.enter_context(patch(target, return_value=_DEFERRED))
        yield Env(rec=rec, cache=cache, jobs=jobs, state=state, repos=repos)


def _complete_job(env: Env, results: int) -> None:
    """The job finishes: its terminal snapshot is written, status completed."""
    from code_indexer.server.services.temporal_snapshot_store import (
        store_temporal_snapshot,
    )

    snapshot = {
        "results": [{"file_path": f"m{i}.py", "score": 0.9} for i in range(results)],
        "shards_completed": 4,
        "shards_total": 4,
        "ctx": {"requested_limit": 10},
    }
    store_temporal_snapshot(env.cache, _JOB_ID, snapshot, terminal=True)
    env.jobs.status = {"status": "completed", "result": {"result_ready": True}}


def _mcp_search(env: Env, username: str) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search import search_code

    env.jobs.owners.append(username)
    response = search_code(
        {
            "repository_alias": "example-repo",
            "query_text": "token refresh",
            "time_range": "2024-01-01..2024-12-31",
            "limit": 10,
        },
        make_user(username),
    )
    body = dict(json.loads(response["content"][0]["text"]))
    assert body["error_code"] == "TEMPORAL_QUERY_DEFERRED", body
    return body


def _mcp_poll(username: str) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers.search import handle_poll_search_job

    response = handle_poll_search_job({"job_id": _JOB_ID}, make_user(username))
    return dict(json.loads(response["content"][0]["text"]))


class TestMcpDeferredTemporal:
    def test_completed_job_is_recorded_once_with_its_real_count(self, env):
        _mcp_search(env, "alice")
        assert env.rec.rows() == []

        _complete_job(env, results=3)
        assert _mcp_poll("alice")["status"] == "completed"
        assert _mcp_poll("alice")["status"] == "completed"

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["result_count"]) == (
            "temporal",
            "alice",
            3,
        )
        assert (row["repo_alias"], row["query_text"]) == (
            "example-repo",
            "token refresh",
        )
        assert row["total_latency_ms"] >= 0

    def test_running_then_failed_job_records_no_row(self, env):
        _mcp_search(env, "alice")

        assert _mcp_poll("alice")["status"] == "waiting"
        env.jobs.status = {"status": "failed", "error": "search failed"}
        assert _mcp_poll("alice")["status"] == "failed"

        assert env.rec.rows() == []

    def test_same_user_repeat_of_a_deferred_search_records_one_row(self, env):
        """The temporal dedup signature includes the username: an identical
        repeat by the same user reuses the job, and its handoff overwrites
        the one pending record -- the repeats collapse into one row."""
        _mcp_search(env, "alice")
        _mcp_search(env, "alice")

        _complete_job(env, results=2)
        _mcp_poll("alice")
        _mcp_poll("alice")

        rows = env.rec.rows(search_type="temporal")
        assert [(r["username"], r["result_count"]) for r in rows] == [("alice", 2)]


def _rest_app(env: Env) -> Any:
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
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user("carol")
    return TestClient(app)


class TestRestDeferredTemporal:
    def test_completed_job_is_recorded_once_by_the_result_endpoint(self, env):
        client = _rest_app(env)
        env.jobs.owners.append("carol")
        response = client.post(
            "/api/query",
            json={
                "query_text": "token refresh",
                "repository_alias": "example-repo",
                "time_range": "2024-01-01..2024-12-31",
                "limit": 10,
            },
        )
        assert response.status_code == 202, response.text
        assert env.rec.rows() == []

        _complete_job(env, results=4)
        for _ in range(2):
            result = client.get(f"/api/query/result/{_JOB_ID}")
            assert result.json()["status"] == "completed", result.text

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["result_count"]) == (
            "temporal",
            "carol",
            4,
        )
