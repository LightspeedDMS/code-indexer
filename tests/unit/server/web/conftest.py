"""Session-scoped DB+admin bootstrap for tests/unit/server/web/.

Most tests in this directory use a `TestClient(app)` fixture (without the
`with` block), which does NOT trigger the FastAPI lifespan. The lifespan is
where the production code creates `<CIDX_SERVER_DATA_DIR>/data/cidx_server.db`
and seeds the initial admin user. Without that bootstrap, `POST /login`
fixtures (`admin_session_cookie`) hit
`sqlite3.connect(self.db_path) -> OperationalError: unable to open database file`.

This conftest creates the schema and seeds admin/admin once at session start
so every test in this chunk has a usable backing DB regardless of whether its
fixture runs the lifespan. The CIDX_SERVER_DATA_DIR resolution mirrors
`DatabaseSchema.__init__` (database_manager.py:603-606).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Generator

import pytest


def _restore_dependency_globals_impl() -> Generator[None, None, None]:
    """Core generator body for `_restore_dependency_globals` below;
    extracted so a unit test can drive it via `next()` without pytest's
    fixture machinery (mirrors the `_impl` pattern established in the
    tree-wide `tests/unit/server/conftest.py`) -- see
    `tests/unit/server/web/test_oidc_state_manager_path_leak_1959.py`.

    Capture and restore process-wide auth singletons around each test.

    Multiple chunk-4 test fixtures call create_app() inside
    patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}). create_app()
    mutates dependencies.{user,jwt,oauth,mcp_credential}_manager module globals
    to point at services bound to the per-test tmpdir. When patch.dict exits
    and the tmpdir is cleaned up, the still-mutated globals point at deleted
    paths, causing subsequent tests to hit OperationalError: unable to open
    database file on POST /login. Restoring the singletons isolates each test.

    The same leak class hits two more process-wide singletons that a real
    FastAPI lifespan wires to the same per-test tmpdir via set_sqlite_path():
    code_indexer.server.app._token_blacklist (via get_token_blacklist()) and
    code_indexer.server.auth.elevated_session_manager.elevated_session_manager.
    Without saving/restoring their path state here too, whichever such test
    runs last in a full suite leaves both pointing at a deleted directory,
    and any later test that exercises them (e.g. DataRetentionScheduler's
    cleanup, which prunes both tables) hits
    sqlite3.OperationalError: unable to open database file.

    Bug #1959: a THIRD singleton of the identical class -- the module-level
    `code_indexer.server.auth.oidc.state_manager._configured_sqlite_path`
    global, written by `configure_sqlite_path()` in the SAME
    `service_init.py` block that wires the two singletons above -- was
    missing from this fixture. Left unrestored, every subsequent
    `StateManager()` construction anywhere in the process (all of
    tests/unit/server/auth/oidc/ and tests/unit/server/auth/oauth/) tries
    to open a SQLite file under a directory that no longer exists,
    surfacing as 47 failures in the exact combined
    `pytest tests/unit/server/web/ tests/unit/server/auth/` run Bug #1959's
    acceptance criteria require to pass.
    """
    from code_indexer.server.auth import dependencies
    from code_indexer.server.app import get_token_blacklist
    from code_indexer.server.auth.elevated_session_manager import (
        elevated_session_manager,
    )
    from code_indexer.server.auth.oidc import state_manager as oidc_state_manager

    token_blacklist = get_token_blacklist()
    saved = {
        "user_manager": dependencies.user_manager,
        "jwt_manager": dependencies.jwt_manager,
        "oauth_manager": dependencies.oauth_manager,
        "mcp_credential_manager": dependencies.mcp_credential_manager,
        "token_blacklist_sqlite_db_path": token_blacklist._sqlite_db_path,
        "elevated_session_manager_db_path": elevated_session_manager._db_path,
        "oidc_configured_sqlite_path": oidc_state_manager._configured_sqlite_path,
    }
    yield
    dependencies.user_manager = saved["user_manager"]
    dependencies.jwt_manager = saved["jwt_manager"]
    dependencies.oauth_manager = saved["oauth_manager"]
    dependencies.mcp_credential_manager = saved["mcp_credential_manager"]
    token_blacklist._sqlite_db_path = saved["token_blacklist_sqlite_db_path"]
    elevated_session_manager._db_path = saved["elevated_session_manager_db_path"]
    oidc_state_manager._configured_sqlite_path = saved["oidc_configured_sqlite_path"]


@pytest.fixture(autouse=True)
def _restore_dependency_globals() -> Generator[None, None, None]:
    """Bug #1959 / prior fix: tree-scoped autouse fixture wrapping
    `_restore_dependency_globals_impl` above. See that function's
    docstring for the full rationale and the measured leak evidence this
    closes.
    """
    yield from _restore_dependency_globals_impl()


@pytest.fixture(autouse=True)
def _bootstrap_server_database():
    """Initialize SQLite schema, seed admin/admin, and ensure auth singletons.

    Function scope so that any test which deletes its tmp dir / cleans up the
    DB on teardown gets a fresh schema for the next test. Schema initialization
    is idempotent (CREATE TABLE IF NOT EXISTS), so the cost is just a few
    milliseconds per test.

    Singleton assignment rationale: tests in this directory hit form-based
    `POST /login`, which checks `dependencies.user_manager` and returns 500
    "User manager not available" if None. The companion `_restore_dependency_globals`
    fixture (above) captures the pre-test singleton state and restores it on
    teardown — which means if the first test in a session inherits a None
    initial state, every subsequent test starts with None as well, and any
    test fixture that does NOT trigger FastAPI lifespan (most tests in this
    directory use `TestClient(app)` without `with`) cannot lazily initialize
    the singletons. Assigning here ensures every test has a working
    `dependencies.user_manager` regardless of lifespan and regardless of
    save/restore order — bootstrap runs after the save, so the new value is
    visible during the test; the restore on teardown is a no-op for the next
    test because the next test's bootstrap overwrites it.
    """
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    db_path = Path(server_data_dir) / "data" / "cidx_server.db"

    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    from code_indexer.server.auth.user_manager import UserManager
    from code_indexer.server.auth import dependencies

    user_manager = UserManager(use_sqlite=True, db_path=str(db_path))
    user_manager.seed_initial_admin()
    dependencies.user_manager = user_manager

    yield
