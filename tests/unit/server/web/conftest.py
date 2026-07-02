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

import pytest


@pytest.fixture(autouse=True)
def _restore_dependency_globals():
    """Capture and restore dependencies.* singletons around each test.

    Multiple chunk-4 test fixtures call create_app() inside
    patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir}). create_app()
    mutates dependencies.{user,jwt,oauth,mcp_credential}_manager module globals
    to point at services bound to the per-test tmpdir. When patch.dict exits
    and the tmpdir is cleaned up, the still-mutated globals point at deleted
    paths, causing subsequent tests to hit OperationalError: unable to open
    database file on POST /login. Restoring the singletons isolates each test.
    """
    from code_indexer.server.auth import dependencies

    saved = {
        "user_manager": dependencies.user_manager,
        "jwt_manager": dependencies.jwt_manager,
        "oauth_manager": dependencies.oauth_manager,
        "mcp_credential_manager": dependencies.mcp_credential_manager,
    }
    yield
    dependencies.user_manager = saved["user_manager"]
    dependencies.jwt_manager = saved["jwt_manager"]
    dependencies.oauth_manager = saved["oauth_manager"]
    dependencies.mcp_credential_manager = saved["mcp_credential_manager"]


@pytest.fixture(autouse=True)
def _bootstrap_server_database():
    """Initialize SQLite schema and seed admin/admin before every test (function-scoped).

    Function scope so that any test which deletes its tmp dir / cleans up the
    DB on teardown gets a fresh schema for the next test. Schema initialization
    is idempotent (CREATE TABLE IF NOT EXISTS), so the cost is just a few
    millisecond per test.
    """
    server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    db_path = Path(server_data_dir) / "data" / "cidx_server.db"

    from code_indexer.server.storage.database_manager import DatabaseSchema

    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()

    from code_indexer.server.auth.user_manager import UserManager

    user_manager = UserManager(use_sqlite=True, db_path=str(db_path))
    user_manager.seed_initial_admin()

    yield
