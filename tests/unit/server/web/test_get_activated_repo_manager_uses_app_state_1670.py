"""
Bug #1670: web/routes.py's _get_activated_repo_manager() resolved activated
repos via the WRONG store in cluster mode.

Confirmed live (real 2-node PostgreSQL cluster): POST
/admin/activated-repos/{username}/{alias}/wiki-toggle failed with HTTP 400
"Repository '...' not found for user '...'" for a repo GET /api/repos on
the SAME node correctly listed as active. Root cause: web/routes.py's
_get_activated_repo_manager() constructed a brand-new
ActivatedRepoManager(data_dir=...) on every call -- never wired with
set_connection_pool() -- so it always fell back to the local per-node
JSON-file store regardless of storage_mode, while the WORKING paths
(GET /api/repos, activation) read/write through the properly-wired
app.state.activated_repo_manager singleton (pool set post-hoc in
lifespan.py).

Fix: resolve the shared singleton from app.state, mirroring this same
file's _get_golden_repo_manager() and the already-correct
routers/activated_repos.py::_get_activated_repo_manager().

Real (no-mock) verification uses a fully isolated, throwaway PostgreSQL
DATABASE (never the database TEST_POSTGRES_DSN's default dbname points
at): a uniquely-named database is CREATEd against the DSN's target
server before the test and DROPped after, so this test can never touch
a real activated_repos table no matter what TEST_POSTGRES_DSN's default
database happens to contain. A real ActivatedRepoManager wired with a
real ConnectionPool exercises the exact psycopg-backed store lookup the
reported bug hit -- per the project's "faithful DB mocks" lesson, not a
mock that would hide the divergence.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Generator

import pytest

from code_indexer.server.web import routes as web_routes


_UNSET = object()


def _restore_app_state_activated_repo_manager():
    from code_indexer.server import app as app_module

    saved = getattr(app_module.app.state, "activated_repo_manager", _UNSET)
    yield app_module
    if saved is _UNSET:
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")
    else:
        app_module.app.state.activated_repo_manager = saved


@pytest.fixture
def app_state_activated_repo_manager_slot() -> Generator[Any, None, None]:
    """Save/restore app.state.activated_repo_manager around a test."""
    gen = _restore_app_state_activated_repo_manager()
    app_module = next(gen)
    try:
        yield app_module
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


class TestGetActivatedRepoManagerResolvesFromAppState:
    """Structural/behavioral proof the helper reads app.state, not a fresh
    unwired construction."""

    def test_returns_the_app_state_singleton(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        from unittest.mock import MagicMock

        app_module = app_state_activated_repo_manager_slot
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        app_module.app.state.activated_repo_manager = sentinel_manager

        result = web_routes._get_activated_repo_manager()

        assert result is sentinel_manager

    def test_raises_runtime_error_when_not_initialized(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        app_module = app_state_activated_repo_manager_slot
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")

        with pytest.raises(
            RuntimeError, match="activated_repo_manager not initialized"
        ):
            web_routes._get_activated_repo_manager()


HAS_PSYCOPG_FOR_LIVE_PG = False
try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoError,
        ActivatedRepoManager,
    )

    HAS_PSYCOPG_FOR_LIVE_PG = True
except ImportError:
    pass


@pytest.fixture(scope="module")
def pg_server_dsn_for_activated_repo_manager():
    """The raw TEST_POSTGRES_DSN, used ONLY to reach the target server for
    provisioning a disposable database -- never to run DDL/DML directly
    against its own default database."""
    if not HAS_PSYCOPG_FOR_LIVE_PG:
        pytest.skip("psycopg not available")
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    try:
        with psycopg.connect(dsn) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    return dsn


@pytest.fixture
def disposable_activated_repos_db(pg_server_dsn_for_activated_repo_manager):
    """Provision a uniquely-named, disposable database on the target
    server (CREATE DATABASE / DROP DATABASE), with the real
    activated_repos table (matching the columns
    ActivatedRepoManager._save_metadata_pg/_load_metadata_pg use) created
    inside it.

    Never touches whatever database TEST_POSTGRES_DSN's dbname points
    at -- so this test cannot destroy a real activated_repos table no
    matter what that default database contains. The try/finally wraps
    CREATE DATABASE through schema setup (not just the yield) so a
    failure partway through setup still triggers DROP DATABASE via the
    db_created guard.
    """
    server_dsn = pg_server_dsn_for_activated_repo_manager
    db_name = f"bug1670_test_{uuid.uuid4().hex[:12]}"
    db_created = False

    try:
        with psycopg.connect(server_dsn, autocommit=True) as admin_conn:
            admin_conn.execute(f'CREATE DATABASE "{db_name}"')
        db_created = True

        info: Dict[str, Any] = dict(conninfo_to_dict(server_dsn))
        info["dbname"] = db_name
        test_dsn = make_conninfo(**info)

        with psycopg.connect(test_dsn, autocommit=True) as conn:
            conn.execute(
                """
                CREATE TABLE activated_repos (
                    username TEXT NOT NULL,
                    user_alias TEXT NOT NULL,
                    golden_repo_alias TEXT,
                    repo_path TEXT,
                    current_branch TEXT,
                    activated_at TIMESTAMPTZ,
                    last_accessed TIMESTAMPTZ,
                    git_committer_email TEXT,
                    ssh_key_used TEXT,
                    is_composite BOOLEAN DEFAULT FALSE,
                    wiki_enabled BOOLEAN DEFAULT FALSE,
                    activation_id TEXT,
                    metadata_json JSONB,
                    PRIMARY KEY (username, user_alias)
                )
                """
            )

        yield test_dsn
    finally:
        if db_created:
            with psycopg.connect(server_dsn, autocommit=True) as admin_conn:
                admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


@pytest.fixture
def wired_activated_repo_manager(disposable_activated_repos_db, tmp_path):
    """A real ActivatedRepoManager wired with a real PostgreSQL
    ConnectionPool against the disposable database -- shared setup for
    the live-PG tests below."""
    pool = ConnectionPool(disposable_activated_repos_db, name="bug1670-live")
    try:
        manager = ActivatedRepoManager(data_dir=str(tmp_path))
        manager.set_connection_pool(pool)
        yield manager
    finally:
        pool.close()


@pytest.fixture
def seeded_wiki_repo_metadata(wired_activated_repo_manager):
    """Seed one activated-repo row via the pool-wired manager, simulating
    the real activation flow that writes through
    app.state.activated_repo_manager."""
    wired_activated_repo_manager._save_metadata(
        "admin",
        "mywiki",
        {
            "golden_repo_alias": "mywiki",
            "path": "/some/path",
            "current_branch": "main",
        },
    )
    return wired_activated_repo_manager


pytestmark_live = pytest.mark.skipif(
    not HAS_PSYCOPG_FOR_LIVE_PG, reason="psycopg not available"
)


@pytestmark_live
class TestSetWikiEnabledFindsRealClusterRow:
    """Bug #1670 core reproduction: a row written through the properly
    pool-wired manager (simulating the working GET /api/repos path) must
    be found by _get_activated_repo_manager()'s resolution."""

    def test_toggle_via_app_state_resolved_manager_succeeds(
        self, seeded_wiki_repo_metadata, app_state_activated_repo_manager_slot
    ) -> None:
        app_module = app_state_activated_repo_manager_slot
        app_module.app.state.activated_repo_manager = seeded_wiki_repo_metadata

        # This is exactly what toggle_user_wiki_enabled does: resolve the
        # manager via the (now-fixed) helper, then call set_wiki_enabled.
        resolved_manager = web_routes._get_activated_repo_manager()
        resolved_manager.set_wiki_enabled("admin", "mywiki", True)

        reloaded = seeded_wiki_repo_metadata._load_metadata("admin", "mywiki")
        assert reloaded is not None
        assert reloaded["wiki_enabled"] is True

    def test_unwired_manager_reproduces_the_original_bug(
        self, seeded_wiki_repo_metadata, tmp_path
    ) -> None:
        """Negative control: proves the pre-fix failure mode (a fresh,
        never-pool-wired ActivatedRepoManager, exactly what the OLD
        _get_activated_repo_manager() constructed) is real and specific
        to pool wiring, not to the schema/fixture setup."""
        unwired = ActivatedRepoManager(data_dir=str(tmp_path))
        with pytest.raises(ActivatedRepoError, match="not found"):
            unwired.set_wiki_enabled("admin", "mywiki", True)
