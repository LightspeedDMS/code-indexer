"""Story #2115: Web UI query page, wiki search and REST multi-search are recorded.

- Web UI query page (POST /admin/query) and its results partial (POST
  /admin/partials/query-results): one row per completed query; the executed
  mode (a temporal query is "temporal", the SCIP mode "scip"), the signed-in
  user, the repository, the query text and the result count.
- Wiki search (GET /wiki/{alias}/_search and the user-wiki form): one row per
  completed search, as the caller.
- REST POST /api/query/multi: one row per searched repository.

Same rule as every door: only a completed, successful search is recorded.

The routes are the real ones; the provider-backed query
(SemanticQueryManager.query_user_repositories) and the multi-repository
engine stand in, as in the other search-event tests. The query double writes
embedding cache fields into the active search event context the way the
real embedding call sites do, proving the door installs one. Rows are read
back from a REAL store (SQLite and PostgreSQL).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional
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


class _QueryManager:
    """SemanticQueryManager stand-in for the provider-backed query: returns
    two results and records a Voyage cache hit on the active search event
    context, as the real embedding call sites do. `fail` raises instead."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def query_user_repositories(self, **kwargs: Any) -> Dict[str, Any]:
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryError,
        )
        from code_indexer.server.services.search_event_context import (
            _search_event_ctx,
        )

        self.calls.append(kwargs)
        if self.fail:
            raise SemanticQueryError("search failed")
        context = _search_event_ctx.get(None)
        if context is not None:
            context.record_provider_cache_fields(
                "voyage-ai", cache_hit=True, cache_mode="active", latency_ms=4
            )
        return {
            "results": [
                {"file_path": f"docs/page_{i}.md", "similarity_score": 0.9}
                for i in range(2)
            ],
            "total_results": 2,
            "query_metadata": {},
        }


class Env(SimpleNamespace):
    rec: Recorder
    tmp: Path


@pytest.fixture(params=_KINDS)
def env(request, tmp_path) -> Iterator[Env]:
    with recorder(request.param, tmp_path) as rec:
        yield Env(rec=rec, tmp=tmp_path)


# ---------------------------------------------------------------------------
# Web UI query page
# ---------------------------------------------------------------------------

PARTIAL = "/admin/partials/query-results"
FULL_PAGE = "/admin/query"
_REPOS = [{"user_alias": "example-repo", "username": "admin", "is_global": False}]


def _web_query(
    env: Env,
    handler: str,
    manager: _QueryManager,
    monkeypatch: Any,
    repos: Optional[List[Dict[str, Any]]] = None,
    **form: Any,
) -> str:
    from fastapi import Response
    from fastapi.testclient import TestClient

    from code_indexer.server import app as app_package
    from code_indexer.server.web import routes as web_routes

    app = app_package.app
    listed = _REPOS if repos is None else repos
    monkeypatch.setattr(
        app.state, "search_event_log_writer", env.rec.writer, raising=False
    )
    monkeypatch.setattr(web_routes, "_get_semantic_query_manager", lambda: manager)
    monkeypatch.setattr(
        web_routes, "_get_all_activated_repos_for_query", lambda registry=None: listed
    )
    monkeypatch.setattr(web_routes, "_add_to_query_history", lambda *a: None)
    session = MagicMock(role="admin", username="admin", csrf_token="t")
    session_manager = MagicMock()
    session_manager.get_session.return_value = session
    session_manager._serializer.secret_key = "unit-test-secret"
    session_manager._serializer.secret_keys = ["unit-test-secret"]
    client = TestClient(app)
    data = {
        "repository": "example-repo (admin)",
        "limit": "10",
        "csrf_token": "example-csrf-token",
        **form,
    }
    with patch(
        "code_indexer.server.web.routes.get_session_manager",
        return_value=session_manager,
    ):
        csrf = Response()
        web_routes.set_csrf_cookie(csrf, "example-csrf-token")
        for header in csrf.headers.getlist("set-cookie"):
            name, _, rest = header.partition("=")
            client.cookies.set(name, rest.split(";", 1)[0])
        response = client.post(handler, data=data)
    assert response.status_code == 200, response.text
    return str(response.text)


class _ScipEngine:
    """SCIPQueryEngine stand-in (the real one needs a generated .scip.db):
    records each query and finds one call chain from -> Middle -> to."""

    def __init__(self, scip_file: Path, *_: Any, **__: Any) -> None:
        self.calls: List[str] = []

    def trace_call_chain(
        self, from_symbol: str, to_symbol: str, **_: Any
    ) -> List[SimpleNamespace]:
        self.calls.append("callchain")
        return [SimpleNamespace(path=[from_symbol, "Middle", to_symbol], length=2)]

    def find_definition(self, *_: Any, **__: Any) -> List[Any]:
        self.calls.append("definition")
        return []


def _use_scip_engine(monkeypatch: Any) -> List[_ScipEngine]:
    """Install _ScipEngine where both Web SCIP paths import it; returns the
    engines the request constructs."""
    from code_indexer.scip.query import primitives

    engines: List[_ScipEngine] = []

    def _make(*args: Any, **kwargs: Any) -> _ScipEngine:
        engines.append(_ScipEngine(*args, **kwargs))
        return engines[-1]

    monkeypatch.setattr(primitives, "SCIPQueryEngine", _make)
    return engines


def _scip_repo_with_index(tmp: Path) -> Dict[str, Any]:
    scip_dir = tmp / "example-repo" / ".code-indexer" / "scip"
    scip_dir.mkdir(parents=True)
    (scip_dir / "index.scip").write_bytes(b"")
    return {
        "user_alias": "example-repo",
        "username": "admin",
        "is_global": False,
        "path": str(tmp / "example-repo"),
    }


class TestWebQueryPage:
    @pytest.mark.parametrize("handler", [PARTIAL, FULL_PAGE])
    @pytest.mark.parametrize("mode", ["semantic", "fts", "hybrid"])
    def test_completed_web_query_records_one_row(self, env, monkeypatch, handler, mode):
        manager = _QueryManager()
        _web_query(
            env,
            handler,
            manager,
            monkeypatch,
            query_text="token refresh",
            search_mode=mode,
        )
        assert len(manager.calls) == 1

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            mode,
            "admin",
            "example-repo",
        )
        assert (row["query_text"], row["result_count"]) == ("token refresh", 2)
        assert bool(row["voyage_cache_hit"]) is True

    @pytest.mark.parametrize("handler", [PARTIAL, FULL_PAGE])
    def test_temporal_web_query_is_recorded_as_temporal(
        self, env, monkeypatch, handler
    ):
        _web_query(
            env,
            handler,
            _QueryManager(),
            monkeypatch,
            query_text="token refresh",
            search_mode="temporal",
        )

        [row] = env.rec.rows()
        assert row["search_type"] == "temporal"

    @pytest.mark.parametrize("handler", [PARTIAL, FULL_PAGE])
    def test_failed_web_query_records_no_row(self, env, monkeypatch, handler):
        html = _web_query(
            env,
            handler,
            _QueryManager(fail=True),
            monkeypatch,
            query_text="token refresh",
            search_mode="semantic",
        )

        assert "Query failed" in html
        assert env.rec.rows() == []

    def test_scip_mode_web_query_records_one_scip_row(self, env, monkeypatch):
        from code_indexer.server.web import routes as web_routes

        rows = [{"file_path": "src/a.py", "scip_symbol": "Calculator"}] * 3
        monkeypatch.setattr(
            web_routes, "_execute_scip_query", lambda *a, **k: (rows, None)
        )
        _web_query(
            env,
            PARTIAL,
            _QueryManager(),
            monkeypatch,
            query_text="Calculator",
            search_mode="scip",
            scip_query_type="definition",
        )

        [row] = env.rec.rows()
        assert (row["search_type"], row["query_text"], row["result_count"]) == (
            "scip",
            "Calculator",
            3,
        )

    @pytest.mark.parametrize("handler", [PARTIAL, FULL_PAGE])
    def test_unknown_scip_query_type_records_no_row(self, env, monkeypatch, handler):
        """No query runs for an unrecognized SCIP query type: the page shows
        no results, and nothing is recorded (not a 0-result search)."""
        engines = _use_scip_engine(monkeypatch)
        _web_query(
            env,
            handler,
            _QueryManager(),
            monkeypatch,
            repos=[_scip_repo_with_index(env.tmp)],
            query_text="Calculator",
            search_mode="scip",
            scip_query_type="not-a-query-type",
        )

        # The request reached the SCIP dispatch, which ran no query.
        assert [e.calls for e in engines] == [[]]
        assert env.rec.rows() == []

    @pytest.mark.parametrize("handler", [PARTIAL, FULL_PAGE])
    def test_scip_callchain_web_query_records_arrow_query_text(
        self, env, monkeypatch, handler
    ):
        """The Web call chain is recorded with the same query_text MCP and
        REST record (scip_callchain_query_text), not the raw form text."""
        from code_indexer.server.services.search_event_recording import (
            scip_callchain_query_text,
        )

        engines = _use_scip_engine(monkeypatch)
        _web_query(
            env,
            handler,
            _QueryManager(),
            monkeypatch,
            repos=[_scip_repo_with_index(env.tmp)],
            query_text="Foo Bar",
            search_mode="scip",
            scip_query_type="callchain",
        )

        assert [e.calls for e in engines] == [["callchain"]]
        [row] = env.rec.rows()
        assert (row["search_type"], row["query_text"], row["result_count"]) == (
            "scip",
            scip_callchain_query_text("Foo", "Bar"),
            1,
        )
        assert row["query_text"] == "Foo -> Bar"

    def test_failed_scip_mode_web_query_records_no_row(self, env, monkeypatch):
        from code_indexer.server.web import routes as web_routes

        monkeypatch.setattr(
            web_routes,
            "_execute_scip_query",
            lambda *a, **k: ([], "No SCIP index found for repository"),
        )
        _web_query(
            env,
            PARTIAL,
            _QueryManager(),
            monkeypatch,
            query_text="Calculator",
            search_mode="scip",
        )

        assert env.rec.rows() == []


# ---------------------------------------------------------------------------
# Wiki search
# ---------------------------------------------------------------------------


def _wiki_get(env: Env, path: str, manager: _QueryManager) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.wiki import routes as wiki_routes

    app = FastAPI()
    app.include_router(wiki_routes.wiki_router, prefix="/wiki")
    app.state.search_event_log_writer = env.rec.writer
    app.state.semantic_query_manager = manager
    app.state.query_tracker = None
    app.state.activated_repo_manager = None
    app.dependency_overrides[wiki_routes.get_current_user_hybrid] = lambda: make_user(
        "frank"
    )
    with patch.object(wiki_routes, "_check_wiki_access", lambda *a: str(env.tmp)):
        with patch.object(
            wiki_routes, "_check_user_wiki_access", lambda *a: str(env.tmp)
        ):
            return TestClient(app).get(path)


class TestWikiSearch:
    @pytest.mark.parametrize(
        "path,repo_alias",
        [
            (
                "/wiki/example-repo/_search?q=token+refresh&mode=fts",
                "example-repo-global",
            ),
            (
                "/wiki/u/frank/example-repo/_search?q=token+refresh&mode=fts",
                "example-repo",
            ),
        ],
    )
    def test_completed_wiki_search_records_one_row(self, env, path, repo_alias):
        response = _wiki_get(env, path, _QueryManager())
        assert response.status_code == 200 and len(response.json()) == 2

        [row] = env.rec.rows()
        assert (row["search_type"], row["username"], row["repo_alias"]) == (
            "fts",
            "frank",
            repo_alias,
        )
        assert (row["query_text"], row["result_count"]) == ("token refresh", 2)

    def test_failed_wiki_search_records_no_row(self, env):
        response = _wiki_get(
            env, "/wiki/example-repo/_search?q=token", _QueryManager(fail=True)
        )

        assert "error" in response.json()
        assert env.rec.rows() == []


# ---------------------------------------------------------------------------
# REST POST /api/query/multi
# ---------------------------------------------------------------------------


class _MultiSearchService:
    """MultiSearchService stand-in (the multi-repository engine is not under
    test): first-repo returned two results, second-repo none, third-repo
    failed. Records a Voyage cache hit on the active context, as the real
    embedding call sites do."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def search(self, request: Any) -> Any:
        from code_indexer.server.multi import MultiSearchResponse
        from code_indexer.server.multi.models import MultiSearchMetadata
        from code_indexer.server.services.search_event_context import (
            _search_event_ctx,
        )

        if self.fail:
            raise RuntimeError("search engine failure")
        context = _search_event_ctx.get(None)
        if context is not None:
            context.record_provider_cache_fields(
                "voyage-ai", cache_hit=False, cache_mode="active", latency_ms=9
            )
        hit = {"file_path": "src/a.py", "score": 0.9, "content": "def a(): pass"}
        return MultiSearchResponse(
            results={"first-repo": [hit, hit], "second-repo": []},
            metadata=MultiSearchMetadata(
                total_results=2, total_repos_searched=2, execution_time_ms=7
            ),
            errors={"third-repo": "Repository not found"},
        )


def _multi_query(env: Env, service: _MultiSearchService) -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes import multi_query_routes

    app = FastAPI()
    app.include_router(multi_query_routes.router)
    app.state.search_event_log_writer = env.rec.writer
    app.state.access_filtering_service = SimpleNamespace(is_admin_user=lambda u: True)
    app.dependency_overrides[get_current_user] = lambda: make_user("gina")
    body = {
        "repositories": ["first-repo", "second-repo", "third-repo"],
        "query": "token refresh",
        "search_type": "semantic",
    }
    with patch.object(multi_query_routes, "get_multi_search_service", lambda: service):
        return TestClient(app).post("/api/query/multi", json=body)


class TestRestMultiQuery:
    def test_multi_query_records_one_row_per_searched_repository(self, env):
        response = _multi_query(env, _MultiSearchService())
        assert response.status_code == 200, response.text

        rows = env.rec.rows(search_type="semantic")
        assert sorted((r["repo_alias"], r["result_count"]) for r in rows) == [
            ("first-repo", 2),
            ("second-repo", 0),
        ]
        assert {(r["username"], r["query_text"]) for r in rows} == {
            ("gina", "token refresh")
        }
        assert all(r["voyage_cache_hit"] in (False, 0) for r in rows)

    def test_failed_multi_query_records_no_row(self, env):
        response = _multi_query(env, _MultiSearchService(fail=True))

        assert response.status_code == 500
        assert env.rec.rows() == []
