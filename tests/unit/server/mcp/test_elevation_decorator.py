"""
Tests for @require_mcp_elevation decorator (Story #925 AC1).

12 tests covering all error paths and success paths:
1. Kill switch off            -> passthrough to handler
2. manager is None            -> elevation_enforcement_disabled (fail closed)
3. TOTP service None          -> elevation_enforcement_disabled
4. TOTP not set up            -> totp_setup_required (with setup_url)
5. No session_key             -> elevation_required
6. No active window           -> elevation_required
7. Recovery scope + full req  -> elevation_required
8. Full scope passes          -> handler called
9. totp_repair scope passes for totp_repair required -> handler called
10. Full scope satisfies totp_repair required (rank check)
11. functools.wraps preserves handler __name__
12. session_key from positional extra arg (non-kwarg callers)

Duplication reduction strategy:
- `manager` fixture: real ElevatedSessionManager with temp DB
- `admin_user` fixture: standard admin User object
- `totp_enabled` / `totp_disabled` fixtures: mock TOTP service
- `decorated` fixture: pre-wrapped _noop_handler with default require_mcp_elevation()
- `active_full_session` / `active_repair_session` fixtures: manager with pre-opened window
- `_patch_all` contextmanager helper: applies enforcement + ESM + TOTP patches together
"""

import contextlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_USERNAME = "admin"
_SESSION_KEY = "jti-test-session-abc"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"


# ---------------------------------------------------------------------------
# Shared handler used in all tests
# ---------------------------------------------------------------------------


def _noop_handler(args, user, session_key=None):
    """Minimal handler recording invocation."""
    return {"success": True, "called": True}


# ---------------------------------------------------------------------------
# Contextmanager helper: applies all three patches together (declared explicitly
# as a contextmanager helper, not a pytest fixture, because it needs parameters)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_all(esm, totp_svc, enforcement=True):
    """Apply enforcement, ESM, and TOTP patches in a single context."""
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    return User(
        username=_USERNAME,
        role="admin",
        password_hash=_DUMMY_HASH,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def manager(tmp_path):
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elev.db"),
    )


@pytest.fixture
def totp_enabled():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


@pytest.fixture
def totp_disabled():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = False
    return svc


@pytest.fixture
def decorated():
    """Pre-wrapped _noop_handler with default require_mcp_elevation()."""
    return require_mcp_elevation()(_noop_handler)


@pytest.fixture
def active_full_session(manager):
    """Manager with a full-scope window already open."""
    manager.create(_SESSION_KEY, _USERNAME, "127.0.0.1", scope="full")
    return manager


@pytest.fixture
def active_repair_session(manager):
    """Manager with a totp_repair-scope window already open."""
    manager.create(_SESSION_KEY, _USERNAME, "127.0.0.1", scope="totp_repair")
    return manager


# ---------------------------------------------------------------------------
# Test 1: Kill switch off -> elevation_enforcement_disabled
# ---------------------------------------------------------------------------


def test_kill_switch_off_passes_through_to_handler(
    decorated, admin_user, manager, totp_enabled
):
    with _patch_all(manager, totp_enabled, enforcement=False):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)
    # Kill switch off -> passthrough, handler runs normally
    assert result["success"] is True
    assert result["called"] is True


# ---------------------------------------------------------------------------
# Test 2: manager is None -> elevation_enforcement_disabled (fail closed)
# ---------------------------------------------------------------------------


def test_manager_none_returns_disabled_error(decorated, admin_user, totp_enabled):
    with _patch_all(None, totp_enabled, enforcement=True):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)
    assert result["error"] == "elevation_enforcement_disabled"


# ---------------------------------------------------------------------------
# Test 3: TOTP service None -> elevation_enforcement_disabled
# ---------------------------------------------------------------------------


def test_totp_service_none_returns_disabled(decorated, admin_user, manager):
    with _patch_all(manager, None, enforcement=True):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)
    assert result["error"] == "elevation_enforcement_disabled"


# ---------------------------------------------------------------------------
# Test 4: TOTP not set up -> totp_setup_required with setup_url
# ---------------------------------------------------------------------------


def test_totp_not_setup_returns_setup_required(
    decorated, admin_user, manager, totp_disabled
):
    with _patch_all(manager, totp_disabled):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)
    assert result["error"] == "totp_setup_required"
    assert result.get("setup_url") == "/admin/mfa/setup"


# ---------------------------------------------------------------------------
# Test 5: No session_key -> elevation_required
# ---------------------------------------------------------------------------


def test_no_session_key_returns_elevation_required(
    decorated, admin_user, manager, totp_enabled
):
    with _patch_all(manager, totp_enabled):
        result = decorated({}, admin_user)  # neither kwarg nor positional
    assert result["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Test 6: No active window -> elevation_required
# ---------------------------------------------------------------------------


def test_no_window_returns_elevation_required(
    decorated, admin_user, manager, totp_enabled
):
    with _patch_all(manager, totp_enabled):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)
    assert result["error"] == "elevation_required"


# ---------------------------------------------------------------------------
# Test 7: Recovery scope + full required -> elevation_required
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_recovery_scope_insufficient_for_full_required(
    admin_user, active_repair_session, totp_enabled
):
    handler = require_mcp_elevation(required_scope="full")(_noop_handler)
    with _patch_all(active_repair_session, totp_enabled):
        result = handler({}, admin_user, session_key=_SESSION_KEY)
    assert result["error"] == "elevation_required"
    assert "full" in result["message"].lower()


# ---------------------------------------------------------------------------
# Test 8: Full scope passes for full required
# ---------------------------------------------------------------------------


def test_full_scope_passes(admin_user, active_full_session, totp_enabled):
    handler = require_mcp_elevation(required_scope="full")(_noop_handler)
    with _patch_all(active_full_session, totp_enabled):
        result = handler({}, admin_user, session_key=_SESSION_KEY)
    assert result["success"] is True
    assert result["called"] is True


# ---------------------------------------------------------------------------
# Test 9: totp_repair scope passes for totp_repair required
# ---------------------------------------------------------------------------


def test_totp_repair_scope_passes_for_repair_required(
    admin_user, active_repair_session, totp_enabled
):
    handler = require_mcp_elevation(required_scope="totp_repair")(_noop_handler)
    with _patch_all(active_repair_session, totp_enabled):
        result = handler({}, admin_user, session_key=_SESSION_KEY)
    assert result["success"] is True
    assert result["called"] is True


# ---------------------------------------------------------------------------
# Test 10: Full scope satisfies totp_repair required (rank check)
# ---------------------------------------------------------------------------


def test_full_scope_passes_for_repair_required(
    admin_user, active_full_session, totp_enabled
):
    handler = require_mcp_elevation(required_scope="totp_repair")(_noop_handler)
    with _patch_all(active_full_session, totp_enabled):
        result = handler({}, admin_user, session_key=_SESSION_KEY)
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Test 11: functools.wraps preserves handler __name__
# ---------------------------------------------------------------------------


def test_decorator_preserves_function_name():
    def my_custom_handler(args, user, session_key=None):
        return {"ok": True}

    decorated_h = require_mcp_elevation()(my_custom_handler)
    assert decorated_h.__name__ == "my_custom_handler"


# ---------------------------------------------------------------------------
# Test 12: session_key from positional extra arg (non-kwarg callers)
# ---------------------------------------------------------------------------


def test_session_key_from_positional_extra(
    decorated, admin_user, active_full_session, totp_enabled
):
    with _patch_all(active_full_session, totp_enabled):
        result = decorated({}, admin_user, _SESSION_KEY)  # positional, not kwarg
    assert result["success"] is True
