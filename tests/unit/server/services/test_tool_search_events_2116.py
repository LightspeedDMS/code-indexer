"""Story #2116: SCIP, git-history and CI log searches are recorded.

Included families, their search_type and what the row means:

- SCIP code intelligence (MCP scip_definition/references/dependencies/
  dependents/impact/context/callchain, REST /scip/*, REST
  /api/scip/multi/*) -> "scip". query_text is the symbol ("from -> to" for a
  call chain); result_count is the number of results returned (results,
  affected symbols, call chains or context files).
- Git history search (MCP git_search_commits, git_search_diffs) ->
  "git_history". query_text is the search query / term; result_count is the
  number of matching commits returned.
- CI log search (MCP ci_search_logs) -> "ci_logs". query_text is the
  pattern; result_count is the number of matching log lines returned.

Cache and provider columns are NULL for all three. Only a search whose
result is known and successful writes a row; a multi-repository search
writes one row per searched repository.

Each test drives the real handler or route and reads the row back from a
REAL store (SQLite and PostgreSQL) written by the REAL writer.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from tests.unit.server._scip_access import GRANT_ALL
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

_SCIP_FIXTURE = Path(__file__).parents[3] / "scip/fixtures/comprehensive_index.scip.db"


class Env(SimpleNamespace):
    rec: Recorder
    golden_repos_dir: Path
    state: SimpleNamespace


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    """A golden repos directory with one repository holding a real SCIP
    index, and the MCP handlers' app state over a real writer."""
    golden = tmp_path / "golden-repos"
    scip_dir = golden / "example-repo" / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    shutil.copy(_SCIP_FIXTURE, scip_dir / "index.scip.db")
    with recorder(request.param, tmp_path) as rec:
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            golden_repos_dir=str(golden),
            access_filtering_service=GRANT_ALL,
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            activated_repo_manager=None,
            golden_repo_manager=None,
        )
        with patch("code_indexer.server.mcp.handlers._utils.app_module", app_module):
            yield Env(rec=rec, golden_repos_dir=golden, state=state)


def _body(response: Dict[str, Any]) -> Dict[str, Any]:
    return dict(json.loads(response["content"][0]["text"]))


# (handler name, arguments, expected query_text, list the results are in)
_MCP_SCIP_CALLS = [
    ("scip_definition", {"symbol": "Calculator"}, "Calculator", "results"),
    ("scip_references", {"symbol": "Calculator"}, "Calculator", "results"),
    ("scip_dependencies", {"symbol": "Calculator"}, "Calculator", "results"),
    ("scip_dependents", {"symbol": "add"}, "add", "results"),
    ("scip_impact", {"symbol": "Calculator"}, "Calculator", "affected_symbols"),
    ("scip_context", {"symbol": "Calculator"}, "Calculator", "files"),
    (
        "scip_callchain",
        {"from_symbol": "Calculator", "to_symbol": "add"},
        "Calculator -> add",
        "chains",
    ),
]


class TestMcpScipSearches:
    @pytest.mark.parametrize("name,args,query_text,result_list", _MCP_SCIP_CALLS)
    def test_each_mcp_scip_search_records_one_scip_row(
        self, env, name, args, query_text, result_list
    ):
        from code_indexer.server.mcp.handlers import scip

        body = _body(
            getattr(scip, name)(
                {**args, "repository_alias": "example-repo"}, make_user("alice")
            )
        )
        assert body["success"] is True, body

        [row] = env.rec.rows()
        # The bare alias is recorded as the global repository the SCIP
        # service resolved it to.
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            "scip",
            "alice",
            "example-repo-global",
        )
        assert row["query_text"] == query_text
        assert row["result_count"] == len(body[result_list])
        assert row["voyage_cache_hit"] is None
        assert row["cohere_cache_hit"] is None

    @pytest.mark.parametrize(
        "name,args",
        [
            ("scip_definition", {}),
            ("scip_dependencies", {"symbol": "Calculator", "depth": 99}),
            ("scip_callchain", {"from_symbol": "", "to_symbol": "add"}),
        ],
    )
    def test_rejected_mcp_scip_search_records_no_row(self, env, name, args):
        from code_indexer.server.mcp.handlers import scip

        body = _body(getattr(scip, name)(args, make_user("alice")))

        assert body["success"] is False
        assert env.rec.rows() == []

    @pytest.mark.parametrize("name,args,query_text,result_list", _MCP_SCIP_CALLS)
    def test_mcp_scip_search_that_fails_records_a_failure_row(
        self, env, name, args, query_text, result_list
    ):
        """Story #2117: a SCIP search that ran and failed is a failure row
        (its exception's class); argument rejections above write nothing."""
        from code_indexer.server.mcp.handlers import scip

        class _FailingService:
            def __getattr__(self, method: str) -> Any:
                def _fail(*_a: Any, **_k: Any) -> Any:
                    raise RuntimeError("SCIP database unreadable")

                return _fail

        with patch.object(scip, "_get_scip_query_service", lambda: _FailingService()):
            body = _body(
                getattr(scip, name)(
                    {**args, "repository_alias": "example-repo"}, make_user("alice")
                )
            )

        assert body["success"] is False, body
        [row] = env.rec.rows()
        assert (row["tool"], row["door"], row["outcome"], row["error_class"]) == (
            name,
            "mcp",
            "failure",
            "RuntimeError",
        )
        assert (row["repo_alias"], row["query_text"]) == ("example-repo", query_text)


def _rest_scip(
    env: Env, path: str, params: Dict[str, Any], access: Any = GRANT_ALL
) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routers.scip_queries import router

    app = FastAPI()
    app.include_router(router)
    app.state.search_event_log_writer = env.rec.writer
    app.state.golden_repos_dir = env.state.golden_repos_dir
    app.state.access_filtering_service = access
    app.dependency_overrides[get_current_user] = lambda: make_user("bob")
    return TestClient(app).get(f"/scip/{path}", params=params)


_REST_SCIP_CALLS = [
    ("definition", {"symbol": "Calculator"}, "Calculator", "results"),
    ("references", {"symbol": "Calculator"}, "Calculator", "results"),
    ("dependencies", {"symbol": "Calculator"}, "Calculator", "results"),
    ("dependents", {"symbol": "add"}, "add", "results"),
    ("impact", {"symbol": "Calculator"}, "Calculator", "affected_symbols"),
    ("context", {"symbol": "Calculator"}, "Calculator", "files"),
    (
        "callchain",
        {"from_symbol": "Calculator", "to_symbol": "add"},
        "Calculator -> add",
        "chains",
    ),
]


class TestRestScipSearches:
    @pytest.mark.parametrize("path,params,query_text,result_list", _REST_SCIP_CALLS)
    def test_each_rest_scip_search_records_one_scip_row(
        self, env, path, params, query_text, result_list
    ):
        response = _rest_scip(env, path, {**params, "project": "example-repo"})
        body = response.json()
        assert response.status_code == 200 and body["success"] is True, body

        [row] = env.rec.rows()
        # Finding 6: the bare project is recorded as the global repository
        # the SCIP service resolved it to.
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            "scip",
            "bob",
            "example-repo-global",
        )
        assert row["query_text"] == query_text
        assert row["result_count"] == len(body[result_list])

    def test_refused_rest_scip_search_records_no_row(self, env):
        response = _rest_scip(env, "definition", {"symbol": "Calculator"}, access=None)

        assert response.status_code == 500, response.text
        assert env.rec.rows() == []

    @pytest.mark.parametrize("path,params,query_text,result_list", _REST_SCIP_CALLS)
    def test_rest_scip_search_that_fails_records_a_failure_row(
        self, env, path, params, query_text, result_list
    ):
        """Story #2117: a REST SCIP search that ran and failed is a failure
        row (its exception's class)."""
        from code_indexer.server.routers import scip_queries

        class _FailingService:
            def __getattr__(self, method: str) -> Any:
                def _fail(*_a: Any, **_k: Any) -> Any:
                    raise RuntimeError("SCIP database unreadable")

                return _fail

        with patch.object(
            scip_queries, "_get_scip_query_service", lambda request: _FailingService()
        ):
            response = _rest_scip(env, path, {**params, "project": "example-repo"})
        assert response.json()["success"] is False, response.text

        [row] = env.rec.rows()
        assert (row["tool"], row["door"], row["outcome"], row["error_class"]) == (
            f"GET /scip/{path}",
            "rest",
            "failure",
            "RuntimeError",
        )
        assert (row["repo_alias"], row["query_text"]) == ("example-repo", query_text)

    def test_rest_callchain_timeout_records_a_failure_row(self, env):
        from code_indexer.server.routers import scip_queries

        class _TimingOutService:
            def trace_callchain(self, *_a: Any, **_k: Any) -> Any:
                return [], ["query timed out"]

        with patch.object(
            scip_queries, "_get_scip_query_service", lambda request: _TimingOutService()
        ):
            response = _rest_scip(
                env,
                "callchain",
                {
                    "from_symbol": "Calculator",
                    "to_symbol": "add",
                    "project": "example-repo",
                },
            )
        assert response.json()["success"] is False, response.text

        [row] = env.rec.rows()
        assert (row["outcome"], row["error_class"], row["query_text"]) == (
            "failure",
            "QueryTimeoutError",
            "Calculator -> add",
        )


def _second_scip_repo(env: Env) -> None:
    """A second golden repository holding the same real SCIP index."""
    scip_dir = env.golden_repos_dir / "second-repo" / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    shutil.copy(_SCIP_FIXTURE, scip_dir / "index.scip.db")


def _assert_one_row_per_repository(
    env: Env, body: Dict[str, Any], result_list: str
) -> None:
    """Both repositories hold the same index, so each contributed half of
    the results; each gets its own row with its own count."""
    total = len(body[result_list])
    assert total > 0 and total % 2 == 0, body
    rows = {r["repo_alias"]: (r["result_count"], r["outcome"]) for r in env.rec.rows()}
    assert rows == {
        "example-repo-global": (total // 2, "success"),
        "second-repo-global": (total // 2, "success"),
    }


# (MCP handler, REST path, arguments, list the results are in)
_NO_ALIAS_CALLS = [
    ("scip_definition", "definition", {"symbol": "Calculator"}, "results"),
    ("scip_references", "references", {"symbol": "Calculator"}, "results"),
]


class TestScipWithoutAlias:
    """D: a SCIP search without an alias searches every SCIP-indexed
    repository the caller may access: one row per repository searched, each
    with that repository's own result count (tallied where the service's
    per-repository loop knows the repository)."""

    @pytest.mark.parametrize("handler,path,args,result_list", _NO_ALIAS_CALLS)
    def test_mcp_records_one_row_per_repository(
        self, env, handler, path, args, result_list
    ):
        from code_indexer.server.mcp.handlers import scip

        _second_scip_repo(env)
        body = _body(getattr(scip, handler)(dict(args), make_user("alice")))
        assert body["success"] is True, body
        _assert_one_row_per_repository(env, body, result_list)

    @pytest.mark.parametrize("handler,path,args,result_list", _NO_ALIAS_CALLS)
    def test_rest_records_one_row_per_repository(
        self, env, handler, path, args, result_list
    ):
        _second_scip_repo(env)
        body = _rest_scip(env, path, dict(args)).json()
        assert body["success"] is True, body
        _assert_one_row_per_repository(env, body, result_list)


class _ScipMultiService:
    """SCIPMultiService stand-in: the multi-repository engine is not under
    test. Answers what the real service answers when first-repo returns two
    results, second-repo was searched and found nothing, third-repo failed."""

    def _response(self, request: Any) -> Any:
        from code_indexer.server.multi.scip_models import (
            SCIPMultiMetadata,
            SCIPMultiResponse,
            SCIPResult,
        )
        from code_indexer.server.services.search_event_context import (
            note_current_repositories,
        )

        # Like the real service: the repositories it resolved.
        note_current_repositories(
            searched=["first-repo", "second-repo"], failed=["third-repo"]
        )

        return SCIPMultiResponse(
            results={
                "first-repo": [
                    SCIPResult(
                        repository="first-repo",
                        symbol=f"s{i}",
                        file_path="src/a.py",
                        line=1,
                        column=0,
                        kind="definition",
                        context=None,
                    )
                    for i in range(2)
                ],
                "second-repo": [],
            },
            metadata=SCIPMultiMetadata(
                total_results=2,
                repos_searched=2,
                repos_with_results=1,
                execution_time_ms=5,
            ),
            skipped={},
            errors={"third-repo": "SCIP definition failed: index unreadable"},
        )

    definition = _response
    callchain = _response


def _rest_scip_multi(env: Env, path: str, body: Dict[str, Any]) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes import scip_multi_routes

    app = FastAPI()
    app.include_router(scip_multi_routes.router)
    app.state.search_event_log_writer = env.rec.writer
    app.state.access_filtering_service = SimpleNamespace(is_admin_user=lambda u: True)
    app.dependency_overrides[get_current_user] = lambda: make_user("carol")
    with patch.object(
        scip_multi_routes, "get_scip_multi_service", lambda: _ScipMultiService()
    ):
        return TestClient(app).post(f"/api/scip/multi/{path}", json=body)


class TestRestScipMultiSearches:
    @pytest.mark.parametrize(
        "path,body,query_text",
        [
            ("definition", {"symbol": "Calculator"}, "Calculator"),
            (
                "callchain",
                {"symbol": "", "from_symbol": "Calculator", "to_symbol": "add"},
                "Calculator -> add",
            ),
        ],
    )
    def test_multi_repository_scip_search_records_one_row_per_searched_repo(
        self, env, path, body, query_text
    ):
        repositories = ["first-repo", "second-repo", "third-repo"]
        response = _rest_scip_multi(env, path, {**body, "repositories": repositories})
        assert response.status_code == 200, response.text

        rows = env.rec.rows(search_type="scip")
        # Story #2117: the repository that failed gets a failure row.
        assert sorted(
            (r["repo_alias"], r["result_count"], r["outcome"]) for r in rows
        ) == [
            ("first-repo", 2, "success"),
            ("second-repo", 0, "success"),
            ("third-repo", 0, "failure"),
        ]
        assert {(r["username"], r["query_text"]) for r in rows} == {
            ("carol", query_text)
        }


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def git_aliases(env, tmp_path) -> Path:
    """A real git repository with two commits, registered under two global
    aliases in the golden repos directory."""
    from code_indexer.global_repos.alias_manager import AliasManager

    repo: Path = tmp_path / "git-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Example Dev")
    (repo / "a.py").write_text("example_value = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "Add example value")
    (repo / "a.py").write_text("example_value = 2\n")
    _git(repo, "commit", "-q", "-am", "Fix token refresh")
    manager = AliasManager(str(env.golden_repos_dir / "aliases"))
    for alias in ("first-repo-global", "second-repo-global"):
        manager.create_alias(alias, str(repo))
    env.state.backend_registry = SimpleNamespace(
        global_repos=SimpleNamespace(get_repo=lambda alias: None)
    )
    env.state.access_filtering_service = SimpleNamespace(
        get_accessible_repos=GRANT_ALL.get_accessible_repos,
        is_admin_user=lambda username: False,
    )
    return repo


class TestGitHistorySearches:
    def test_git_search_commits_records_one_git_history_row(self, env, git_aliases):
        from code_indexer.server.mcp.handlers.git_read import (
            handle_git_search_commits,
        )

        body = _body(
            handle_git_search_commits(
                {"repository_alias": "first-repo-global", "query": "token"},
                make_user("dana"),
            )
        )
        assert body["success"] is True and len(body["matches"]) == 1, body

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            "git_history",
            "dana",
            "first-repo-global",
        )
        assert (row["query_text"], row["result_count"]) == ("token", 1)
        assert row["voyage_cache_hit"] is None

    def test_omni_git_search_commits_records_one_row_per_repository(
        self, env, git_aliases
    ):
        from code_indexer.server.mcp.handlers.git_read import (
            handle_git_search_commits,
        )

        body = _body(
            handle_git_search_commits(
                {
                    "repository_alias": [
                        "first-repo-global",
                        "second-repo-global",
                        "unknown-repo-global",
                    ],
                    "query": "example",
                },
                make_user("dana"),
            )
        )
        assert body["repos_searched"] == 2, body

        rows = env.rec.rows(search_type="git_history")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo-global", 1),
            ("second-repo-global", 1),
        ]

    def test_git_search_diffs_records_one_git_history_row(self, env, git_aliases):
        from code_indexer.server.mcp.handlers.git_read import handle_git_search_diffs

        body = _body(
            handle_git_search_diffs(
                {
                    "repository_alias": "first-repo-global",
                    "search_string": "example_value",
                },
                make_user("dana"),
            )
        )
        assert body["success"] is True, body

        [row] = env.rec.rows()
        assert (row["search_type"], row["query_text"]) == (
            "git_history",
            "example_value",
        )
        assert row["result_count"] == len(body["matches"]) > 0

    @pytest.mark.parametrize(
        "handler_name, method, args",
        [
            ("git_search_commits", "search_commits", {"query": "token"}),
            ("git_search_diffs", "search_diffs", {"search_string": "example_value"}),
        ],
    )
    def test_git_search_that_fails_records_a_failure_row(
        self, env, git_aliases, handler_name, method, args
    ):
        """Story #2117: the repository resolved, then the git search failed
        (the git operations layer is the stand-in)."""
        from code_indexer.global_repos.git_operations import GitOperationsService
        from code_indexer.server.mcp.handlers import git_read

        def _fail(self: Any, **_: Any) -> Any:
            raise RuntimeError("git failed")

        handler = getattr(git_read, f"handle_{handler_name}")
        with patch.object(GitOperationsService, method, _fail):
            body = _body(
                handler(
                    {"repository_alias": "first-repo-global", **args},
                    make_user("dana"),
                )
            )

        assert body["success"] is False, body
        [row] = env.rec.rows()
        assert (row["tool"], row["door"], row["outcome"], row["error_class"]) == (
            handler_name,
            "mcp",
            "failure",
            "RuntimeError",
        )
        assert (row["search_type"], row["repo_alias"]) == (
            "git_history",
            "first-repo-global",
        )

    def test_failed_git_search_records_no_row(self, env, git_aliases):
        from code_indexer.server.mcp.handlers.git_read import (
            handle_git_search_commits,
        )

        body = _body(
            handle_git_search_commits(
                {"repository_alias": "unknown-repo-global", "query": "token"},
                make_user("dana"),
            )
        )

        assert body["success"] is False
        assert env.rec.rows() == []


class _ForgeClient:
    """The external CI forge (GitHub Actions) client: not under test."""

    last_rate_limit = None
    fail = False

    def __init__(self, token: str, **_: Any) -> None:
        pass

    async def search_logs(self, **_: Any) -> Any:
        if self.fail:
            raise RuntimeError("forge unreachable")
        return [{"job": "build", "line": 3, "text": "error: example"}] * 2


def _ci_search(env: Env, fail: bool) -> Dict[str, Any]:
    import asyncio

    from code_indexer.server.mcp.handlers import cicd

    target = ("github", "example-org/example-repo", None, "github.com", "token")
    client = type("Client", (_ForgeClient,), {"fail": fail})
    with patch.object(cicd, "_resolve_ci_target", lambda *a: (target, None)):
        with patch(
            "code_indexer.server.clients.github_actions_client.GitHubActionsClient",
            client,
        ):
            response = asyncio.run(
                cicd.handle_ci_search_logs(
                    {
                        "repository_alias": "example-repo-global",
                        "run_id": 7,
                        "pattern": "error",
                    },
                    make_user("erin"),
                )
            )
    return _body(response)


class TestCiLogSearches:
    def test_ci_search_logs_records_one_ci_logs_row(self, env):
        body = _ci_search(env, fail=False)
        assert body["success"] is True, body

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            "ci_logs",
            "erin",
            "example-repo-global",
        )
        assert (row["query_text"], row["result_count"]) == ("error", 2)

    def test_failed_ci_search_logs_records_a_failure_row(self, env):
        """Story #2117: the CI target resolved, then the forge search failed."""
        body = _ci_search(env, fail=True)

        assert body["success"] is False
        [row] = env.rec.rows()
        assert (row["tool"], row["door"], row["outcome"], row["error_class"]) == (
            "ci_search_logs",
            "mcp",
            "failure",
            "RuntimeError",
        )
        assert (row["search_type"], row["repo_alias"]) == (
            "ci_logs",
            "example-repo-global",
        )


_EMPTY_DEFINITION = {
    "success": True,
    "symbol": "Calculator",
    "total_results": 0,
    "results": [],
}


class TestScipUnresolvedSearchesRecordNothing:
    """Finding 5: a SCIP search that resolved no repository with a SCIP index
    -- an unknown or inaccessible alias, or no SCIP index anywhere -- searched
    nothing: no row. The answer is unchanged (an empty success)."""

    def test_mcp_unknown_alias(self, env):
        from code_indexer.server.mcp.handlers import scip

        body = _body(
            scip.scip_definition(
                {"symbol": "Calculator", "repository_alias": "unknown-global"},
                make_user("alice"),
            )
        )

        assert body == _EMPTY_DEFINITION
        assert env.rec.rows() == []

    @pytest.mark.parametrize("name,args,query_text,result_list", _MCP_SCIP_CALLS)
    def test_every_mcp_scip_tool_with_an_unknown_alias(
        self, env, name, args, query_text, result_list
    ):
        from code_indexer.server.mcp.handlers import scip

        body = _body(
            getattr(scip, name)(
                {**args, "repository_alias": "unknown-global"}, make_user("alice")
            )
        )

        assert body["success"] is True and body[result_list] == [], body
        assert env.rec.rows() == []

    @pytest.mark.parametrize("path,params,query_text,result_list", _REST_SCIP_CALLS)
    def test_every_rest_scip_endpoint_with_an_unknown_alias(
        self, env, path, params, query_text, result_list
    ):
        response = _rest_scip(env, path, {**params, "project": "unknown-global"})

        body = response.json()
        assert response.status_code == 200 and body["success"] is True, body
        assert body[result_list] == []
        assert env.rec.rows() == []

    def test_rest_unknown_alias(self, env):
        response = _rest_scip(
            env, "definition", {"symbol": "Calculator", "project": "unknown-global"}
        )

        assert response.status_code == 200, response.text
        assert response.json() == _EMPTY_DEFINITION
        assert env.rec.rows() == []

    def test_alias_free_without_any_scip_index(self, env):
        from code_indexer.server.mcp.handlers import scip

        shutil.rmtree(env.golden_repos_dir / "example-repo" / ".code-indexer")

        mcp_body = _body(
            scip.scip_definition({"symbol": "Calculator"}, make_user("alice"))
        )
        rest = _rest_scip(env, "definition", {"symbol": "Calculator"})

        assert mcp_body == _EMPTY_DEFINITION
        assert rest.status_code == 200, rest.text
        assert rest.json() == _EMPTY_DEFINITION
        assert env.rec.rows() == []
