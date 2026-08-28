"""
Fixtures for auth tests to ensure clean state between tests.

Following CLAUDE.md principles: Real implementations, no mocks.
"""

import pytest
from code_indexer.server.auth.rate_limiter import (
    password_change_rate_limiter,
    refresh_token_rate_limiter,
)
from code_indexer.server.auth.session_manager import session_manager
from code_indexer.server.app import get_token_blacklist


def _reset_token_blacklist() -> None:
    """Reset the process-wide TokenBlacklist singleton to a clean, DB-less
    state (#1698).

    initialize_services() (service_init.py) calls get_token_blacklist().
    set_sqlite_path(str(db_path)) on EVERY create_app() call, binding this
    SHARED singleton's SQLite path to whatever CIDX_SERVER_DATA_DIR temp
    directory that particular test used. When that test's cleanup deletes
    the temp directory (as RealComponentTestInfrastructure and several
    other fixtures do), the singleton is left pointing at a deleted path
    -- and ANY later test that authenticates a JWT (which universally
    checks is_token_blacklisted()) crashes with sqlite3.OperationalError:
    unable to open database file, regardless of whether that later test
    itself ever touches CIDX_SERVER_DATA_DIR. Resetting to None here is
    safe: tests that call create_app() re-set it themselves via
    service_init.py; tests that don't fall back to the harmless
    in-memory-only _local set path.
    """
    blacklist = get_token_blacklist()
    blacklist._local.clear()
    blacklist._sqlite_db_path = None
    blacklist._pool = None


@pytest.fixture(autouse=True)
def reset_singletons():
    """
    Reset all singleton instances before each test to ensure clean state.

    This is critical for tests that rely on rate limiting, session management,
    and token tracking which use global singleton instances.
    """
    # Clear rate limiter state
    password_change_rate_limiter._attempts.clear()
    refresh_token_rate_limiter._attempts.clear()

    # Clear session manager state
    session_manager._invalidated_sessions.clear()
    session_manager._password_change_timestamps.clear()

    # Clear token blacklist state (#1698)
    _reset_token_blacklist()

    yield

    # Clean up after test as well
    password_change_rate_limiter._attempts.clear()
    refresh_token_rate_limiter._attempts.clear()
    session_manager._invalidated_sessions.clear()
    session_manager._password_change_timestamps.clear()
    _reset_token_blacklist()
