"""
Bug #1665 regression guard: WikiCachePostgresBackend is constructed by
StorageFactory but never reaches any of the 6 production WikiCache(...)
call sites, so a multi-node PostgreSQL/HAProxy cluster deployment silently
uses per-node SQLite for the wiki cache -- a page cached by one node is
invisible to a request routed to a different node.

WikiCache.__init__ already fully supports a pluggable
`storage_backend` -- every data method branches on `self._backend`. The gap
is purely that all 6 production call sites construct `WikiCache(db_path)`
with no `storage_backend` argument, so `self._backend` is always None
regardless of storage_mode. The fix threads
`registry_factory.resolve_backend_registry_attr("wiki_cache", ...)`'s
result into each call site as `WikiCache(db_path, storage_backend=...)`.

These tests must FAIL before the fix (every constructed WikiCache's
`_backend` is None even in postgres/cluster mode) and PASS after.

Call sites covered:
  1. wiki/routes.py::_get_wiki_cache (module-level singleton)
  2. repositories/golden_repo_manager.py::remove_golden_repo (lifecycle hook)
  3. mcp/handlers/guides.py::_get_wiki_cache_for_handler
  4. web/routes.py::toggle_wiki_enabled
  5. web/routes.py::refresh_wiki_cache
  6. web/routes.py::toggle_user_wiki_enabled

For sites 1 and 3, the test's system under test IS the helper function
(_get_wiki_cache / _get_wiki_cache_for_handler) and WikiCache is allowed to
construct for real -- the test inspects the genuine `._backend` attribute
on the returned instance. For sites 2, 4, 5, 6 the system under test is the
enclosing lifecycle-hook / route-handler function; WikiCache there is an
external collaborator dependency (imported from another module), so it is
mocked at its import boundary and the constructor call arguments are
asserted -- standard interaction-based testing of a collaborator, not of
the code under test.

Plus characterization tests (TestCrossNodeVisibility*) verifying
WikiCachePostgresBackend's own sharing behavior in isolation: two
WikiCachePostgresBackend instances sharing one underlying store see each
other's writes, while two SQLite-solo WikiCache instances (different
db_path, no storage_backend -- today's default) do NOT. These are unit
tests of the backend/cache classes themselves, run entirely against a
hand-rolled fake pool -- they exercise neither StorageFactory nor any of
the 6 production call sites above, so they have no discriminating power
for the #1665 wiring gap and are NOT a regression guard for it (they
passed unchanged against the pre-fix code). The per-call-site tests above
(Sites 1-6) are the actual #1665 regression guards.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Literal, Optional, Tuple
from unittest.mock import MagicMock, call, patch

import pytest

from code_indexer.server.wiki.wiki_cache import WikiCache
from code_indexer.server.storage.postgres.wiki_cache_backend import (
    WikiCachePostgresBackend,
)


# ---------------------------------------------------------------------------
# app.state postgres-mode helper (mirrors the established pattern in
# tests/unit/server/test_registry_factory_cluster.py's _app_state_postgres_mode)
# ---------------------------------------------------------------------------


class _FakeBackendRegistry:
    """Minimal BackendRegistry-like object exposing only `wiki_cache`."""

    def __init__(self, wiki_cache_backend: Optional[Any]) -> None:
        self.wiki_cache = wiki_cache_backend


_UNSET = object()


@contextlib.contextmanager
def _app_state_wiki_context(
    *,
    postgres_mode: bool = False,
    wiki_cache_backend: Optional[Any] = None,
    golden_repo_manager: Optional[Any] = None,
) -> Generator[None, None, None]:
    """Temporarily configure the REAL server app's app.state for
    resolve_backend_registry_attr("wiki_cache", ...) resolution, restoring
    the exact prior state (including "attribute never existed") on exit.

    `golden_repo_manager`, when supplied, is set on app.state.golden_repo_manager
    -- the attribute web/routes.py's _get_golden_repo_manager() reads.
    """
    from code_indexer.server import app as app_module

    attrs = ("storage_mode", "backend_registry", "golden_repo_manager")
    saved = {a: getattr(app_module.app.state, a, _UNSET) for a in attrs}

    try:
        if postgres_mode:
            app_module.app.state.storage_mode = "postgres"
            app_module.app.state.backend_registry = _FakeBackendRegistry(
                wiki_cache_backend
            )
        if golden_repo_manager is not None:
            app_module.app.state.golden_repo_manager = golden_repo_manager
        yield
    finally:
        for attr, value in saved.items():
            if value is _UNSET:
                if hasattr(app_module.app.state, attr):
                    delattr(app_module.app.state, attr)
            else:
                setattr(app_module.app.state, attr, value)


# ---------------------------------------------------------------------------
# Site 1: wiki/routes.py::_get_wiki_cache (module-level singleton)
#
# System under test: _get_wiki_cache(). WikiCache is allowed to construct
# for real -- no mocking of WikiCache itself.
# ---------------------------------------------------------------------------


class TestSite1WikiRoutesSingleton:
    def test_uses_shared_postgres_backend_in_cluster_mode(self, tmp_path):
        from code_indexer.server.wiki.routes import _get_wiki_cache, _reset_wiki_cache

        _reset_wiki_cache()
        fake_backend = MagicMock(name="fake_wiki_backend")
        request = MagicMock()
        request.app.state.golden_repo_manager.db_path = str(tmp_path / "singleton.db")

        try:
            with _app_state_wiki_context(
                postgres_mode=True, wiki_cache_backend=fake_backend
            ):
                cache = _get_wiki_cache(request)

            assert cache._backend is fake_backend
        finally:
            _reset_wiki_cache()

    def test_backend_is_none_in_solo_mode(self, tmp_path):
        from code_indexer.server.wiki.routes import _get_wiki_cache, _reset_wiki_cache

        _reset_wiki_cache()
        request = MagicMock()
        request.app.state.golden_repo_manager.db_path = str(
            tmp_path / "singleton_solo.db"
        )

        try:
            cache = _get_wiki_cache(request)
            assert cache._backend is None
        finally:
            _reset_wiki_cache()


# ---------------------------------------------------------------------------
# Site 2: repositories/golden_repo_manager.py::remove_golden_repo lifecycle hook
#
# System under test: GoldenRepoManager.remove_golden_repo()'s background
# worker. WikiCache is an external collaborator imported from another
# module -- mocked at its import boundary; the assertion inspects the
# constructor call arguments the hook passed to it.
# ---------------------------------------------------------------------------


class TestSite2GoldenRepoManagerLifecycleHook:
    @pytest.fixture
    def manager(self, tmp_path):
        from code_indexer.server.repositories.golden_repo_manager import (
            GoldenRepoManager,
        )
        from code_indexer.server.storage.database_manager import DatabaseSchema
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        mgr = GoldenRepoManager(data_dir=str(tmp_path))
        mock_bg = MagicMock(spec=BackgroundJobManager)
        mock_bg.submit_job.return_value = "job-1665"
        mgr.background_job_manager = mock_bg
        DatabaseSchema(mgr.db_path).initialize_database()
        return mgr

    def _register_repo(self, manager, alias: str) -> str:
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

        clone_path = os.path.join(manager.golden_repos_dir, alias)
        os.makedirs(clone_path, exist_ok=True)
        golden_repo = GoldenRepo(
            alias=alias,
            repo_url=f"https://github.com/test/{alias}.git",
            default_branch="main",
            clone_path=clone_path,
            created_at=datetime.now(timezone.utc).isoformat(),
            enable_temporal=False,
            temporal_options=None,
        )
        manager.golden_repos[alias] = golden_repo
        manager._sqlite_backend.add_repo(
            alias=golden_repo.alias,
            repo_url=golden_repo.repo_url,
            default_branch=golden_repo.default_branch,
            clone_path=golden_repo.clone_path,
            created_at=golden_repo.created_at,
            enable_temporal=golden_repo.enable_temporal,
            temporal_options=golden_repo.temporal_options,
        )
        return clone_path

    def _run_removal_worker(self, manager, alias: str) -> None:
        manager.remove_golden_repo(alias)
        worker = manager.background_job_manager.submit_job.call_args[1]["func"]
        worker()

    def test_removal_hook_uses_shared_postgres_backend_in_cluster_mode(self, manager):
        self._register_repo(manager, "wiki-cluster-repo")
        fake_backend = MagicMock(name="fake_wiki_backend")

        with patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache:
            with _app_state_wiki_context(
                postgres_mode=True, wiki_cache_backend=fake_backend
            ):
                self._run_removal_worker(manager, "wiki-cluster-repo")

        MockWikiCache.assert_called_once_with(
            manager.db_path, storage_backend=fake_backend
        )

    def test_removal_hook_backend_none_in_solo_mode(self, manager):
        self._register_repo(manager, "wiki-solo-repo")

        with patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache:
            self._run_removal_worker(manager, "wiki-solo-repo")

        MockWikiCache.assert_called_once_with(manager.db_path, storage_backend=None)


# ---------------------------------------------------------------------------
# Site 3: mcp/handlers/guides.py::_get_wiki_cache_for_handler
#
# System under test: _get_wiki_cache_for_handler(). WikiCache is allowed to
# construct for real -- no mocking of WikiCache itself.
# ---------------------------------------------------------------------------


class TestSite3McpGuidesHandler:
    @contextlib.contextmanager
    def _with_golden_repo_manager(self, grm_mock) -> Generator[None, None, None]:
        from code_indexer.server import app as app_module

        saved = getattr(app_module, "golden_repo_manager", _UNSET)
        try:
            app_module.golden_repo_manager = grm_mock
            yield
        finally:
            if saved is _UNSET:
                if hasattr(app_module, "golden_repo_manager"):
                    delattr(app_module, "golden_repo_manager")
            else:
                app_module.golden_repo_manager = saved

    def test_uses_shared_postgres_backend_in_cluster_mode(self, tmp_path):
        from code_indexer.server.mcp.handlers.guides import (
            _get_wiki_cache_for_handler,
        )

        fake_backend = MagicMock(name="fake_wiki_backend")
        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "guides.db")

        with self._with_golden_repo_manager(grm_mock):
            with _app_state_wiki_context(
                postgres_mode=True, wiki_cache_backend=fake_backend
            ):
                cache = _get_wiki_cache_for_handler()

        assert cache is not None
        assert cache._backend is fake_backend

    def test_backend_is_none_in_solo_mode(self, tmp_path):
        from code_indexer.server.mcp.handlers.guides import (
            _get_wiki_cache_for_handler,
        )

        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "guides_solo.db")

        with self._with_golden_repo_manager(grm_mock):
            cache = _get_wiki_cache_for_handler()

        assert cache is not None
        assert cache._backend is None


# ---------------------------------------------------------------------------
# Sites 4-6: web/routes.py route handlers
#
# System under test: the route-handler functions themselves. WikiCache is
# an external collaborator imported from another module -- mocked at its
# import boundary; assertions inspect the constructor call arguments.
# ---------------------------------------------------------------------------


class TestSites4to6WebRoutes:
    def _make_session(self):
        from code_indexer.server.web.auth import SessionData
        import time

        return SessionData(
            username="admin", role="admin", csrf_token="tok", created_at=time.time()
        )

    # -- Site 4: toggle_wiki_enabled -------------------------------------

    def test_toggle_wiki_enabled_uses_shared_postgres_backend_in_cluster_mode(
        self, tmp_path
    ):
        from code_indexer.server.web import routes as web_routes

        fake_backend = MagicMock(name="fake_wiki_backend")
        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "toggle.db")
        grm_mock.golden_repos_dir = str(tmp_path / "golden-repos")
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_create_golden_repos_page_response",
                return_value=MagicMock(),
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(
                postgres_mode=True,
                wiki_cache_backend=fake_backend,
                golden_repo_manager=grm_mock,
            ):
                web_routes.toggle_wiki_enabled(
                    request, "some-alias", wiki_enabled="1", csrf_token="tok"
                )

        assert MockWikiCache.call_args == call(
            grm_mock.db_path, storage_backend=fake_backend
        )

    def test_toggle_wiki_enabled_backend_none_in_solo_mode(self, tmp_path):
        from code_indexer.server.web import routes as web_routes

        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "toggle_solo.db")
        grm_mock.golden_repos_dir = str(tmp_path / "golden-repos-solo")
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_create_golden_repos_page_response",
                return_value=MagicMock(),
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(golden_repo_manager=grm_mock):
                web_routes.toggle_wiki_enabled(
                    request, "some-alias", wiki_enabled="1", csrf_token="tok"
                )

        assert MockWikiCache.call_args == call(grm_mock.db_path, storage_backend=None)

    # -- Site 5: refresh_wiki_cache ---------------------------------------

    def test_refresh_wiki_cache_uses_shared_postgres_backend_in_cluster_mode(
        self, tmp_path
    ):
        from code_indexer.server.web import routes as web_routes

        fake_backend = MagicMock(name="fake_wiki_backend")
        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "refresh.db")
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_create_golden_repos_page_response",
                return_value=MagicMock(),
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(
                postgres_mode=True,
                wiki_cache_backend=fake_backend,
                golden_repo_manager=grm_mock,
            ):
                web_routes.refresh_wiki_cache(request, "some-alias", csrf_token="tok")

        assert MockWikiCache.call_args == call(
            grm_mock.db_path, storage_backend=fake_backend
        )

    def test_refresh_wiki_cache_backend_none_in_solo_mode(self, tmp_path):
        from code_indexer.server.web import routes as web_routes

        grm_mock = MagicMock()
        grm_mock.db_path = str(tmp_path / "refresh_solo.db")
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_create_golden_repos_page_response",
                return_value=MagicMock(),
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(golden_repo_manager=grm_mock):
                web_routes.refresh_wiki_cache(request, "some-alias", csrf_token="tok")

        assert MockWikiCache.call_args == call(grm_mock.db_path, storage_backend=None)

    # -- Site 6: toggle_user_wiki_enabled -----------------------------------

    def test_toggle_user_wiki_enabled_uses_shared_postgres_backend_in_cluster_mode(
        self, tmp_path
    ):
        from code_indexer.server.web import routes as web_routes

        fake_backend = MagicMock(name="fake_wiki_backend")
        golden_grm_mock = MagicMock()
        golden_grm_mock.db_path = str(tmp_path / "golden.db")
        activated_grm_mock = MagicMock()
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_get_activated_repo_manager",
                return_value=activated_grm_mock,
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(
                postgres_mode=True,
                wiki_cache_backend=fake_backend,
                golden_repo_manager=golden_grm_mock,
            ):
                web_routes.toggle_user_wiki_enabled(
                    request,
                    "someuser",
                    "some-alias",
                    wiki_enabled="0",
                    csrf_token="tok",
                )

        assert MockWikiCache.call_args == call(
            golden_grm_mock.db_path, storage_backend=fake_backend
        )

    def test_toggle_user_wiki_enabled_backend_none_in_solo_mode(self, tmp_path):
        from code_indexer.server.web import routes as web_routes

        golden_grm_mock = MagicMock()
        golden_grm_mock.db_path = str(tmp_path / "golden_solo.db")
        activated_grm_mock = MagicMock()
        request = MagicMock()

        with (
            patch.object(
                web_routes, "_require_admin_session", return_value=self._make_session()
            ),
            patch.object(web_routes, "validate_login_csrf_token", return_value=True),
            patch.object(
                web_routes,
                "_get_activated_repo_manager",
                return_value=activated_grm_mock,
            ),
            patch("code_indexer.server.wiki.wiki_cache.WikiCache") as MockWikiCache,
        ):
            with _app_state_wiki_context(golden_repo_manager=golden_grm_mock):
                web_routes.toggle_user_wiki_enabled(
                    request,
                    "someuser",
                    "some-alias",
                    wiki_enabled="0",
                    csrf_token="tok",
                )

        assert MockWikiCache.call_args == call(
            golden_grm_mock.db_path, storage_backend=None
        )


# ---------------------------------------------------------------------------
# Cross-node visibility: proves the postgres-vs-sqlite dispatch is
# conditional, not an accidental universal side effect.
# ---------------------------------------------------------------------------


class _SharedRowStore:
    """Dict-backed row store simulating the wiki_cache/wiki_sidebar_cache
    tables, keyed exactly the way the real SQL keys them. Two
    FakeWikiConnectionPool instances constructed with the SAME
    _SharedRowStore simulate two cluster nodes sharing one PostgreSQL
    database; two pools with DIFFERENT stores simulate two isolated
    databases."""

    def __init__(self) -> None:
        self.articles: Dict[Tuple[str, str], Tuple[Any, ...]] = {}
        self.sidebars: Dict[str, Tuple[Any, ...]] = {}


class _FakeCursor:
    def __init__(self, rows: List[Tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> Optional[Tuple[Any, ...]]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[Tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, store: _SharedRowStore) -> None:
        self._store = store

    def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> _FakeCursor:
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith(
            "SELECT rendered_html, title, file_mtime, file_size, metadata FROM wiki_cache"
        ):
            repo_alias, article_path = params
            row = self._store.articles.get((repo_alias, article_path))
            return _FakeCursor([row] if row is not None else [])
        if sql_norm.startswith("INSERT INTO wiki_cache"):
            (
                repo_alias,
                article_path,
                html,
                title,
                file_mtime,
                file_size,
                rendered_at,
                metadata_json,
            ) = params
            self._store.articles[(repo_alias, article_path)] = (
                html,
                title,
                file_mtime,
                file_size,
                metadata_json,
            )
            return _FakeCursor([])
        if sql_norm.startswith("SELECT sidebar_json FROM wiki_sidebar_cache"):
            (repo_alias,) = params
            row = self._store.sidebars.get(repo_alias)
            return _FakeCursor([row] if row is not None else [])
        if sql_norm.startswith("INSERT INTO wiki_sidebar_cache"):
            repo_alias, sidebar_json, max_mtime, built_at = params
            self._store.sidebars[repo_alias] = (sidebar_json,)
            return _FakeCursor([])
        raise AssertionError(f"Unexpected SQL in fake wiki cache pool: {sql_norm!r}")

    def commit(self) -> None:
        pass


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeConn:
        return self._conn

    def __exit__(self, *exc_info: Any) -> Literal[False]:
        return False


class FakeWikiConnectionPool:
    """Minimal stand-in for storage.postgres.connection_pool.ConnectionPool
    -- `.connection()` returns a context manager yielding a `_FakeConn`
    bound to the given `_SharedRowStore`."""

    def __init__(self, store: _SharedRowStore) -> None:
        self._store = store

    def connection(self) -> _FakeConnCtx:
        return _FakeConnCtx(_FakeConn(self._store))


class TestCrossNodeVisibilityPostgresSharedPool:
    """Characterization test verifying WikiCachePostgresBackend's own
    sharing behavior in isolation (mocked pool, always runs) -- NOT a
    #1665 regression guard: it exercises WikiCachePostgresBackend directly
    against a hand-rolled fake pool, never StorageFactory or any of the 6
    production call sites, so it passed unchanged against the pre-fix
    buggy code. See the per-call-site tests above (Sites 1-6) for the
    actual #1665 regression guards. Two WikiCachePostgresBackend instances
    sharing one underlying row store (simulating two cluster nodes talking
    to the SAME PostgreSQL database) must see each other's writes."""

    def test_shared_pool_backends_see_each_others_writes(self):
        store = _SharedRowStore()
        backend_a = WikiCachePostgresBackend(FakeWikiConnectionPool(store))
        backend_b = WikiCachePostgresBackend(FakeWikiConnectionPool(store))

        backend_a.put_article(
            "shared-repo",
            "docs/article",
            "<html>Node A wrote this</html>",
            "Title A",
            111.0,
            222,
            "2026-08-24T00:00:00",
            None,
        )

        result = backend_b.get_article("shared-repo", "docs/article")

        assert result is not None
        assert result["rendered_html"] == "<html>Node A wrote this</html>"

    def test_different_pools_do_not_share_writes(self):
        """Negative control on the postgres side: two backends against
        DIFFERENT stores (simulating two unrelated databases) must NOT see
        each other's writes -- confirms the shared-store fixture above is
        actually discriminating, not vacuously true."""
        backend_a = WikiCachePostgresBackend(FakeWikiConnectionPool(_SharedRowStore()))
        backend_b = WikiCachePostgresBackend(FakeWikiConnectionPool(_SharedRowStore()))

        backend_a.put_article(
            "isolated-repo",
            "docs/article",
            "<html>Node A wrote this</html>",
            "Title A",
            111.0,
            222,
            "2026-08-24T00:00:00",
            None,
        )

        assert backend_b.get_article("isolated-repo", "docs/article") is None


class TestCrossNodeVisibilitySqliteSoloUnchanged:
    """Negative control proving SQLite-solo behavior is genuinely
    unchanged: two WikiCache instances constructed against two DIFFERENT
    db_path temp files (no storage_backend -- exactly today's default) must
    NOT see each other's writes."""

    def test_different_db_paths_do_not_share_writes(self, tmp_path):
        db_path_a = str(tmp_path / "node_a.db")
        db_path_b = str(tmp_path / "node_b.db")
        cache_a = WikiCache(db_path_a)
        cache_a.ensure_tables()
        cache_b = WikiCache(db_path_b)
        cache_b.ensure_tables()

        article_file = tmp_path / "article.md"
        article_file.write_text("# Article content")

        cache_a.put_article(
            "solo-repo", "article", "<html>A</html>", "Title A", article_file
        )

        assert cache_b.get_article("solo-repo", "article", article_file) is None
        # Sanity: the SAME instance that wrote it can read it back.
        result_a = cache_a.get_article("solo-repo", "article", article_file)
        assert result_a is not None
        assert result_a["html"] == "<html>A</html>"
