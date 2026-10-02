"""POST /api/query searches only repositories the caller can access.

Covers the REST semantic (sync and async), FTS and hybrid branches end to
end through the real routes (POST /api/query, GET /api/jobs/{id},
GET /api/query/result/{id}), the real SemanticQueryManager, the real global
registry, real Tantivy FTS indexes on disk, a real activation and a real
AccessFilteringService. Only the external embedding + HNSW boundary is
replaced (see query_repo_access_env).
"""

from __future__ import annotations

import json
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.inline_jobs import register_job_routes
from code_indexer.server.routers.inline_query import register_query_routes
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from tests.unit.server.query.query_repo_access_env import (
    ADMIN,
    ALL_REPOS,
    ALL_ROWS_LIMIT,
    GRANTED_REPO,
    GRANTED_REPOS,
    OWN_ACTIVATION,
    ROWS_PER_REPO,
    UNGRANTED_REPO,
    USER,
    QueryAccessEnv,
    build_server_db_template,
    global_alias,
)

_TERMINAL = {"completed", "failed", "cancelled"}


def _user(username: str, role: UserRole) -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = role
    return user


NON_ADMIN = _user(USER, UserRole.NORMAL_USER)
ADMIN_USER = _user(ADMIN, UserRole.ADMIN)
# A second non-admin who does not own the first user's jobs.
OTHER_NON_ADMIN = _user("example_other_user", UserRole.NORMAL_USER)


def _fts_index_dir(repo_path: Path) -> Path:
    """Where the query path reads a repo's FTS index."""
    return repo_path / ".code-indexer" / "tantivy_index"


def _build_fts_index(index_dir: Path, repo: str) -> None:
    manager = TantivyIndexManager(index_dir=index_dir)
    manager.initialize_index()
    body = f"def authenticate(user): return '{repo}'"
    manager.add_document(
        {
            "path": f"src/{repo}_auth.py",
            "content": body,
            "content_raw": body,
            "identifiers": ["authenticate", "user"],
            "line_start": 1,
            "line_end": 1,
            "language": "python",
        }
    )
    manager.commit()
    # Drop the writer and await merges so the files on disk are final.
    manager.close()


@pytest.fixture(scope="module")
def fts_index_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build each repo's real Tantivy FTS index once per module.

    A committed, closed Tantivy index is immutable segment files plus
    meta.json naming them by segment id (no absolute paths), so a plain
    file copy is a valid index at its new location. Each test copies these
    instead of paying a fsync'd Tantivy commit per repo.
    """
    template = tmp_path_factory.mktemp("fts_index_template")
    for repo in ALL_REPOS:
        _build_fts_index(template / repo, repo)
    return template


@pytest.fixture(scope="module")
def server_db_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_server_db_template(tmp_path_factory.mktemp("server_db_template"))


@pytest.fixture
def env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server_db_template: Path,
    fts_index_template: Path,
) -> Iterator[QueryAccessEnv]:
    # No embedding provider key: deterministic primary-only routing and no
    # path can reach a real provider (the search boundary is faked).
    monkeypatch.delenv("CO_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    e = QueryAccessEnv(tmp_path, server_db_template)
    # UNGRANTED_REPO is registered first, so it is the first FTS candidate.
    for repo in ALL_REPOS:
        shutil.copytree(fts_index_template / repo, _fts_index_dir(e.repo_paths[repo]))
    try:
        yield e
    finally:
        e.close()


@contextmanager
def _client(
    env: QueryAccessEnv, user: User, access_service: Optional[Any]
) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.payload_cache = env.payload_cache
    app.state.search_event_log_writer = None
    app.state.query_tracker = None
    app.state.access_filtering_service = access_service
    app.state.backend_registry = env.app_stand_in(None).state.backend_registry
    app.state.golden_repos_dir = env.golden_repo_manager.golden_repos_dir
    app.state.background_job_manager = env.job_manager
    register_query_routes(
        app,
        semantic_query_manager=env.query_manager,
        activated_repo_manager=env.activated_repo_manager,
    )
    register_job_routes(
        app,
        jwt_manager=None,
        user_manager=None,
        background_job_manager=env.job_manager,
        job_tracker=None,
    )
    app.dependency_overrides[dependencies.get_current_user] = lambda: user
    app.dependency_overrides[dependencies.get_current_user_hybrid] = lambda: user
    with env.installed(access_service):
        yield TestClient(app, raise_server_exceptions=False)


def _post(env: QueryAccessEnv, user: User, body: Dict[str, Any]) -> Any:
    with _client(env, user, env.access_service) as client:
        return client.post("/api/query", json=body)


def _poll_job(client: TestClient, job_id: str) -> Dict[str, Any]:
    """Poll GET /api/jobs/{id} until terminal (bounded)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        body: Dict[str, Any] = resp.json()
        if body["status"] in _TERMINAL:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within 30s")


def _repos_of(rows: List[Dict[str, Any]]) -> Set[str]:
    return {r["repository_alias"] for r in rows}


def _assert_access_control_unavailable(resp: Any) -> None:
    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"]["error_code"] == "access_control_unavailable"


class TestSemanticSync:
    def test_non_admin_gets_full_limit_and_granted_only_metadata(self, env):
        resp = _post(env, NON_ADMIN, {"query_text": "find", "limit": ROWS_PER_REPO})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["results"]) == ROWS_PER_REPO
        assert _repos_of(body["results"]) == {global_alias(GRANTED_REPO)}
        assert body["query_metadata"]["repositories_searched"] == len(GRANTED_REPOS)

    def test_explicit_ungranted_alias_is_refused_like_an_unknown_alias(self, env):
        ungranted = _post(
            env,
            NON_ADMIN,
            {"query_text": "find", "repository_alias": global_alias(UNGRANTED_REPO)},
        )
        unknown = _post(
            env,
            NON_ADMIN,
            {"query_text": "find", "repository_alias": "no-such-repo-global"},
        )

        assert ungranted.status_code == unknown.status_code == 404
        assert env.searched_paths == []

    def test_admin_searches_every_global_repo(self, env):
        resp = _post(env, ADMIN_USER, {"query_text": "find", "limit": ALL_ROWS_LIMIT})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _repos_of(body["results"]) == {global_alias(r) for r in ALL_REPOS}
        assert body["query_metadata"]["repositories_searched"] == len(ALL_REPOS)


class TestAsyncFrontDoor:
    def test_non_admin_async_result_has_granted_rows_only_on_both_endpoints(self, env):
        granted = {global_alias(r) for r in GRANTED_REPOS}
        with _client(env, NON_ADMIN, env.access_service) as client:
            submit = client.post(
                "/api/query",
                json={
                    "query_text": "find",
                    "limit": ALL_ROWS_LIMIT,
                    "async_query": True,
                },
            )
            assert submit.status_code == 202, submit.text
            job_id = submit.json()["job_id"]
            job = _poll_job(client, job_id)
            result_resp = client.get(f"/api/query/result/{job_id}")

        assert job["status"] == "completed", job
        assert _repos_of(job["result"]["results"]) == granted
        assert job["result"]["query_metadata"]["repositories_searched"] == len(
            GRANTED_REPOS
        )
        assert result_resp.status_code == 200, result_resp.text
        polled = result_resp.json()
        assert polled["status"] == "completed"
        assert _repos_of(polled["results"]) == granted
        assert polled["query_metadata"]["repositories_searched"] == len(GRANTED_REPOS)
        assert "degraded_repos" not in polled["query_metadata"]
        assert UNGRANTED_REPO not in json.dumps(job)
        assert UNGRANTED_REPO not in result_resp.text

    def test_other_user_cannot_read_the_job_on_either_endpoint(self, env):
        with _client(env, NON_ADMIN, env.access_service) as owner:
            submit = owner.post(
                "/api/query",
                json={"query_text": "find", "async_query": True},
            )
            assert submit.status_code == 202, submit.text
            job_id = submit.json()["job_id"]
            assert _poll_job(owner, job_id)["status"] == "completed"

        with _client(env, OTHER_NON_ADMIN, env.access_service) as other:
            jobs_resp = other.get(f"/api/jobs/{job_id}")
            result_resp = other.get(f"/api/query/result/{job_id}")

        assert jobs_resp.status_code == 404, jobs_resp.text
        assert result_resp.status_code == 404, result_resp.text
        assert result_resp.json()["status"] == "not_found"
        for resp in (jobs_resp, result_resp):
            assert not any(repo in resp.text for repo in ALL_REPOS), resp.text


class TestFtsAndHybrid:
    def test_fts_reads_a_granted_repo_index(self, env):
        resp = _post(
            env, NON_ADMIN, {"query_text": "authenticate", "search_mode": "fts"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fts_results"], "FTS must read an index the caller can access"
        assert _repos_of(body["fts_results"]) == {global_alias(GRANTED_REPO)}
        assert all(UNGRANTED_REPO not in r["path"] for r in body["fts_results"])
        assert body["metadata"]["repositories_searched"] == len(GRANTED_REPOS)

    def test_hybrid_counts_and_reads_only_granted_repos(self, env):
        resp = _post(
            env,
            NON_ADMIN,
            {
                "query_text": "authenticate",
                "search_mode": "hybrid",
                "limit": ALL_ROWS_LIMIT,
            },
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _repos_of(body["fts_results"]) == {global_alias(GRANTED_REPO)}
        assert UNGRANTED_REPO not in env.searched_repos()
        assert body["metadata"]["repositories_searched"] == len(GRANTED_REPOS)

    def test_fts_explicit_ungranted_alias_is_not_found(self, env):
        resp = _post(
            env,
            NON_ADMIN,
            {
                "query_text": "authenticate",
                "search_mode": "fts",
                "repository_alias": global_alias(UNGRANTED_REPO),
            },
        )

        assert resp.status_code == 404, resp.text

    def test_admin_fts_selection_is_unchanged(self, env):
        resp = _post(
            env, ADMIN_USER, {"query_text": "authenticate", "search_mode": "fts"}
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert _repos_of(body["fts_results"]) == {global_alias(UNGRANTED_REPO)}
        assert body["metadata"]["repositories_searched"] == len(ALL_REPOS)


class TestMissingAccessServiceRest:
    """No access service: refused whenever a global repo would be searched
    (admins included); an own-activation-only query still runs."""

    @pytest.mark.parametrize("user", [NON_ADMIN, ADMIN_USER], ids=["user", "admin"])
    @pytest.mark.parametrize("search_mode", ["semantic", "fts", "hybrid"])
    def test_default_query_is_refused(self, env, user, search_mode):
        with _client(env, user, None) as client:
            resp = client.post(
                "/api/query",
                json={"query_text": "authenticate", "search_mode": search_mode},
            )

        _assert_access_control_unavailable(resp)
        assert env.searched_paths == []

    def test_explicit_global_alias_is_refused(self, env):
        with _client(env, NON_ADMIN, None) as client:
            resp = client.post(
                "/api/query",
                json={
                    "query_text": "find",
                    "repository_alias": global_alias(GRANTED_REPO),
                },
            )

        _assert_access_control_unavailable(resp)

    def test_own_activation_alias_still_runs_sync(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        with _client(env, NON_ADMIN, None) as client:
            resp = client.post(
                "/api/query",
                json={"query_text": "find", "repository_alias": OWN_ACTIVATION},
            )

        assert resp.status_code == 200, resp.text
        assert _repos_of(resp.json()["results"]) == {OWN_ACTIVATION}

    def test_async_default_query_fails_without_rows(self, env):
        with _client(env, NON_ADMIN, None) as client:
            submit = client.post(
                "/api/query", json={"query_text": "find", "async_query": True}
            )
            assert submit.status_code == 202, submit.text
            job = _poll_job(client, submit.json()["job_id"])

        assert job["status"] == "failed", job
        assert job["result"] is None
        assert "access control" in job["error"]
        assert env.searched_paths == []

    def test_async_failure_read_on_both_endpoints_has_no_rows_or_repo_names(self, env):
        with _client(env, NON_ADMIN, None) as client:
            submit = client.post(
                "/api/query", json={"query_text": "find", "async_query": True}
            )
            assert submit.status_code == 202, submit.text
            job_id = submit.json()["job_id"]
            job = _poll_job(client, job_id)
            jobs_resp = client.get(f"/api/jobs/{job_id}")
            result_resp = client.get(f"/api/query/result/{job_id}")

        assert job["status"] == "failed", job
        assert jobs_resp.status_code == 200, jobs_resp.text
        assert jobs_resp.json()["result"] is None
        assert result_resp.status_code == 200, result_resp.text
        polled = result_resp.json()
        assert polled["status"] == "failed"
        assert "access control" in polled["error"]
        assert "results" not in polled
        for resp in (jobs_resp, result_resp):
            assert not any(repo in resp.text for repo in ALL_REPOS), resp.text
        assert env.searched_paths == []

    def test_async_own_activation_alias_still_runs(self, env):
        env.activate_for(USER, GRANTED_REPO, OWN_ACTIVATION)
        with _client(env, NON_ADMIN, None) as client:
            submit = client.post(
                "/api/query",
                json={
                    "query_text": "find",
                    "repository_alias": OWN_ACTIVATION,
                    "async_query": True,
                },
            )
            assert submit.status_code == 202, submit.text
            job = _poll_job(client, submit.json()["job_id"])

        assert job["status"] == "completed", job
        assert _repos_of(job["result"]["results"]) == {OWN_ACTIVATION}
