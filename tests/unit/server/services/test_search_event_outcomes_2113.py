"""Bug #2113: a search event records what really happened.

Rule (one rule for every door): a row is written only for a search whose
result is known and successful. A failed or rejected search writes no row;
an asynchronous search writes its row when its job completes, with the real
result count -- never a "success with 0 results" at submission.

Each test drives a real front door and reads the REAL store (SQLite and
PostgreSQL) through the REAL SearchEventLogWriter. The REST async test runs
the REAL SemanticQueryManager.submit_query_job wrapper; only its
provider-backed query_user_repositories is replaced, and the job runs on a
BackgroundJobManager stand-in that calls it as the real manager does.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    FakeBackgroundJobManager,
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


class _ActivatedRepos:
    """ActivatedRepoManager stand-in with one activated repository."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self.activated_repos_dir = str(repo.parent)

    def list_activated_repositories(self, username: str) -> List[Dict[str, Any]]:
        return [{"user_alias": "example-repo", "username": username}]

    def get_activated_repo_path(self, username: str, alias: str) -> str:
        return str(self._repo)

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return True


def _three_results(**kwargs: Any) -> Dict[str, Any]:
    """Stands in for the provider-backed query_user_repositories."""
    return {
        "results": [{"file_path": f"src/m{i}.py"} for i in range(3)],
        "total_results": 3,
        "query_metadata": {},
    }


class Env(SimpleNamespace):
    rec: Recorder
    repo: Path
    tmp: Path


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    repo = tmp_path / "repo"
    repo.mkdir()
    with recorder(request.param, tmp_path) as rec:
        yield Env(rec=rec, repo=repo, tmp=tmp_path)


def _submit_async_query(
    env: "Env", monkeypatch: Any, jobs: FakeBackgroundJobManager, query: Any
) -> None:
    """POST an async /api/query through the REAL SemanticQueryManager job
    wrapper; `query` stands in for the provider-backed query_user_repositories.
    Asserts the submission answered 202 and recorded nothing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies
    from code_indexer.server.routers.inline_query import register_query_routes

    repos = _ActivatedRepos(env.repo)
    manager = SemanticQueryManager(
        data_dir=str(env.tmp / "data"),
        activated_repo_manager=repos,  # type: ignore[arg-type]
        background_job_manager=jobs,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(manager, "query_user_repositories", query)
    app = FastAPI()
    register_query_routes(
        app, semantic_query_manager=manager, activated_repo_manager=repos
    )
    app.state.search_event_log_writer = env.rec.writer
    app.dependency_overrides[dependencies.get_current_user] = lambda: make_user("alice")

    response = TestClient(app).post(
        "/api/query",
        json={
            "query_text": "token refresh",
            "repository_alias": "example-repo",
            "async_query": True,
        },
    )
    assert response.status_code == 202, response.text
    assert env.rec.rows() == []


class TestRestAsyncQueryThatDidNotComplete:
    def test_async_query_cancelled_while_running_records_no_row(self, env, monkeypatch):
        jobs = FakeBackgroundJobManager()

        def _cancelled_while_searching(**kwargs: Any) -> Dict[str, Any]:
            assert jobs.running_job_id is not None
            jobs.cancel(jobs.running_job_id)
            return _three_results(**kwargs)

        _submit_async_query(env, monkeypatch, jobs, _cancelled_while_searching)
        jobs.run_pending()

        assert len(jobs.cancelled) == 1
        assert env.rec.rows() == []

    def test_async_query_that_raises_records_no_row(self, env, monkeypatch):
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )

        jobs = FakeBackgroundJobManager()

        def _failing_search(**_: Any) -> Dict[str, Any]:
            raise SemanticQueryError("search failed")

        _submit_async_query(env, monkeypatch, jobs, _failing_search)
        with pytest.raises(SemanticQueryError):
            jobs.run_pending()

        assert env.rec.rows() == []


class TestRestAsyncQuery:
    def test_async_query_is_recorded_when_its_job_completes(self, env, monkeypatch):
        jobs = FakeBackgroundJobManager()
        _submit_async_query(env, monkeypatch, jobs, _three_results)

        jobs.run_pending()

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["result_count"]) == (
            "semantic",
            "alice",
            3,
        )
        assert row["repo_alias"] == "example-repo"
        assert row["query_text"] == "token refresh"


class _ProvidersUnavailable:
    """SemanticQueryManager stand-in for the provider-backed query only.

    Does what the real _perform_search does when every embedding provider is
    down (Bug #1804): records the completeness marker on the caller's
    out-param, then raises SemanticQueryError.
    """

    def query_user_repositories(self, **kwargs: Any) -> Dict[str, Any]:
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )

        out = kwargs["_provider_completeness_out"]
        out["completeness"] = "providers_unavailable"
        out["provider_errors"] = {"voyage-ai": "provider unavailable"}
        raise SemanticQueryError("every configured embedding provider is unavailable")


def _mcp_app_module(env: "Env", **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                search_event_log_writer=env.rec.writer,
                golden_repos_dir=str(env.tmp / "golden-repos"),
                backend_registry=SimpleNamespace(
                    global_repos=SimpleNamespace(list_repos=lambda: {})
                ),
                access_filtering_service=None,
            )
        ),
        activated_repo_manager=_ActivatedRepos(env.repo),
        golden_repo_manager=None,
        **extra,
    )


class TestMcpSearchThatNeverRan:
    """Bug #2113: success true without a search result records nothing."""

    def test_providers_unavailable_records_no_row(self, env, monkeypatch):
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.services.config_service import get_config_service

        monkeypatch.setattr(
            get_config_service().get_config().memory_retrieval_config,
            "memory_retrieval_enabled",
            False,
        )
        app_module = _mcp_app_module(
            env, semantic_query_manager=_ProvidersUnavailable()
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            response = search_code(
                {"repository_alias": "example-repo", "query_text": "token refresh"},
                make_user("alice"),
            )
        body = json.loads(response["content"][0]["text"])

        assert body["success"] is True, body
        completeness = body["results"]["query_metadata"]["completeness"]
        assert completeness == "providers_unavailable"
        assert env.rec.rows() == []

    def test_omni_search_where_every_repository_failed_records_no_row(self, env):
        from code_indexer.server.mcp.handlers._utils import (
            _format_omni_response,
            _mcp_response,
        )
        from code_indexer.server.mcp.handlers.search import search_code

        def _every_repository_failed(params: Any, user: Any) -> Dict[str, Any]:
            """The envelope _omni_search_code builds when MultiSearchService
            searched no repository successfully (the real service needs the
            whole server app, which this test does not start)."""
            formatted = _format_omni_response(
                all_results=[],
                response_format="grouped",
                total_repos_searched=0,
                errors={
                    alias: "Repository or index not found"
                    for alias in params["repository_alias"]
                },
            )
            return _mcp_response({"success": True, "results": formatted})

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", _mcp_app_module(env)
        ):
            with patch(
                "code_indexer.server.mcp.handlers.search.code_search._omni_search_code",
                _every_repository_failed,
            ):
                response = search_code(
                    {
                        "repository_alias": ["first-repo-global", "second-repo-global"],
                        "query_text": "token refresh",
                        "search_mode": "fts",
                    },
                    make_user("alice"),
                )
        body = json.loads(response["content"][0]["text"])

        assert body["success"] is True, body
        assert body["results"]["total_repos_searched"] == 0, body
        assert body["results"]["errors"], body
        assert env.rec.rows() == []


class TestMcpOmniResultCount:
    def test_grouped_omni_search_records_its_real_result_count(self, env):
        """A multi-repository search answers grouped by default; its row
        carries the real number of results, not 0."""
        from code_indexer.server.mcp.handlers._utils import (
            _format_omni_response,
            _mcp_response,
        )
        from code_indexer.server.mcp.handlers.search import search_code

        def _two_repositories_searched(params: Any, user: Any) -> Dict[str, Any]:
            formatted = _format_omni_response(
                all_results=[
                    {"file_path": "a.py", "source_repo": "first-repo-global"},
                    {"file_path": "b.py", "source_repo": "first-repo-global"},
                    {"file_path": "c.py", "source_repo": "second-repo-global"},
                ],
                response_format="grouped",
                total_repos_searched=2,
                errors={},
            )
            return _mcp_response({"success": True, "results": formatted})

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", _mcp_app_module(env)
        ):
            with patch(
                "code_indexer.server.mcp.handlers.search.code_search._omni_search_code",
                _two_repositories_searched,
            ):
                search_code(
                    {
                        "repository_alias": ["first-repo-global", "second-repo-global"],
                        "query_text": "token refresh",
                        "search_mode": "fts",
                    },
                    make_user("alice"),
                )

        [row] = env.rec.rows()
        assert (row["search_type"], row["result_count"]) == ("fts", 3)


class TestMcpHandledError:
    def test_handled_error_records_no_row(self, env):
        from code_indexer.server.mcp.handlers.search import search_code

        app_module = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    search_event_log_writer=env.rec.writer,
                    golden_repos_dir=str(env.tmp / "golden-repos"),
                    backend_registry=SimpleNamespace(
                        global_repos=SimpleNamespace(list_repos=lambda: {})
                    ),
                    access_filtering_service=None,
                )
            ),
            activated_repo_manager=None,
            golden_repo_manager=None,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            response = search_code(
                {
                    "repository_alias": "unknown-repo-global",
                    "query_text": "token refresh",
                },
                make_user("alice"),
            )
        body = json.loads(response["content"][0]["text"])

        assert body["success"] is False, body
        assert "not found" in body["error"]
        assert env.rec.rows() == []
