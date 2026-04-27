"""
Tests for elevate_session MCP tool (Story #925 AC3).

8 tests:
1. Valid TOTP code -> window opened (verified via manager.get_status), response
   elevated=True, scope=full, positive float elevated_until and max_until,
   max_until >= elevated_until
2. Wrong TOTP code -> elevation_failed error
3. Valid recovery code -> window opened with scope=totp_repair (verified via
   manager), same timestamp assertions as test 1
4. Both totp_code and recovery_code provided -> ambiguous_code error
5. Neither code provided -> missing_code error
6. TOTP not enabled for user -> totp_setup_required error with setup_url
7. Kill switch off -> elevation_enforcement_disabled error
8. Rate limiter locked -> rate_limited error

Duplication reduction:
- `admin_user` / `manager` / `totp_svc` / `rate_limiter_unlocked` /
  `rate_limiter_locked` fixtures encapsulate common state
- `_patch_env` contextmanager applies all four patches together
- `_call_elevate` helper encodes the call convention
- `_assert_success_response` extracts the symmetric success-path checks
"""

import contextlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_USERNAME = "admin"
_SESSION_KEY = "mcp-session-jti-abc"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800
_VALID_TOTP = "123456"
_VALID_RECOVERY = "ABCD-1234-EFGH"

_ENFORCEMENT_PATH = "code_indexer.server.mcp.handlers.admin.elevate_session._is_elevation_enforcement_enabled"
_TOTP_PATH = "code_indexer.server.mcp.handlers.admin.elevate_session.get_totp_service"
_ESM_PATH = (
    "code_indexer.server.mcp.handlers.admin.elevate_session.elevated_session_manager"
)
_RATE_LIMITER_PATH = (
    "code_indexer.server.mcp.handlers.admin.elevate_session.login_rate_limiter"
)


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
def totp_svc():
    """Mock TOTP service: MFA enabled, valid TOTP/recovery succeeds."""
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    svc.verify_enabled_code.side_effect = lambda username, code: code == _VALID_TOTP
    svc.verify_recovery_code.side_effect = (
        lambda username, code, ip_address=None: code == _VALID_RECOVERY
    )
    return svc


@pytest.fixture
def rate_limiter_unlocked():
    """Mock rate limiter: not locked."""
    rl = MagicMock()
    rl.is_locked.return_value = (False, 0)
    return rl


@pytest.fixture
def rate_limiter_locked():
    """Mock rate limiter: locked."""
    rl = MagicMock()
    rl.is_locked.return_value = (True, 30)
    return rl


# ---------------------------------------------------------------------------
# Contextmanager: patch all four dependencies together
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_env(manager, totp_svc, rate_limiter, enforcement=True):
    """Apply enforcement, ESM, TOTP service, and rate limiter patches."""
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ESM_PATH, manager),
        patch(_TOTP_PATH, return_value=totp_svc),
        patch(_RATE_LIMITER_PATH, rate_limiter),
    ):
        yield


# ---------------------------------------------------------------------------
# Call helper: invokes elevate_session inside patched environment
# ---------------------------------------------------------------------------


def _call_elevate(
    args, user, session_key, manager, totp_svc, rate_limiter, enforcement=True
):
    from code_indexer.server.mcp.handlers.admin import elevate_session as _elevate

    with _patch_env(manager, totp_svc, rate_limiter, enforcement):
        return _elevate.elevate_session(args, user, session_key)


# ---------------------------------------------------------------------------
# Shared assertion helper for both success paths
# ---------------------------------------------------------------------------


def _assert_success_response(result, expected_scope, manager):
    """Assert response and manager state for a successful elevation."""
    assert result.get("elevated") is True
    assert result.get("scope") == expected_scope
    elevated_until = result.get("elevated_until")
    max_until = result.get("max_until")
    assert isinstance(elevated_until, float) and elevated_until > 0.0
    assert isinstance(max_until, float) and max_until > 0.0
    assert max_until >= elevated_until, "max_until must be >= elevated_until"
    session = manager.get_status(_SESSION_KEY)
    assert session is not None, "No elevation window found in manager"
    assert session.scope == expected_scope
    assert session.username == _USERNAME


# ---------------------------------------------------------------------------
# Test 1: Valid TOTP code -> window opened, response correct, timestamps valid
# ---------------------------------------------------------------------------


def test_valid_totp_opens_full_window(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {"totp_code": _VALID_TOTP},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
    )
    _assert_success_response(result, "full", manager)


# ---------------------------------------------------------------------------
# Test 2: Wrong TOTP code -> elevation_failed
# ---------------------------------------------------------------------------


def test_wrong_totp_returns_elevation_failed(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {"totp_code": "000000"},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
    )
    assert result.get("error") == "elevation_failed"


# ---------------------------------------------------------------------------
# Test 3: Valid recovery code -> window opened with scope=totp_repair
# ---------------------------------------------------------------------------


def test_valid_recovery_code_opens_repair_window(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {"recovery_code": _VALID_RECOVERY},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
    )
    _assert_success_response(result, "totp_repair", manager)


# ---------------------------------------------------------------------------
# Test 4: Both codes provided -> ambiguous_code
# ---------------------------------------------------------------------------


def test_both_codes_returns_ambiguous_code(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {"totp_code": _VALID_TOTP, "recovery_code": _VALID_RECOVERY},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
    )
    assert result.get("error") == "ambiguous_code"


# ---------------------------------------------------------------------------
# Test 5: Neither code provided -> missing_code
# ---------------------------------------------------------------------------


def test_no_code_returns_missing_code(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
    )
    assert result.get("error") == "missing_code"


# ---------------------------------------------------------------------------
# Test 6: TOTP not enabled -> totp_setup_required with setup_url
# ---------------------------------------------------------------------------


def test_totp_not_enabled_returns_setup_required(
    admin_user, manager, rate_limiter_unlocked
):
    totp_not_setup = MagicMock()
    totp_not_setup.is_mfa_enabled.return_value = False
    result = _call_elevate(
        {"totp_code": _VALID_TOTP},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_not_setup,
        rate_limiter_unlocked,
    )
    assert result.get("error") == "totp_setup_required"
    assert "setup_url" in result


# ---------------------------------------------------------------------------
# Test 7: Kill switch off -> elevation_enforcement_disabled
# ---------------------------------------------------------------------------


def test_kill_switch_off_returns_disabled(
    admin_user, manager, totp_svc, rate_limiter_unlocked
):
    result = _call_elevate(
        {"totp_code": _VALID_TOTP},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_unlocked,
        enforcement=False,
    )
    assert result.get("error") == "elevation_enforcement_disabled"


# ---------------------------------------------------------------------------
# Test 8: Rate limiter locked -> rate_limited
# ---------------------------------------------------------------------------


def test_rate_limited_returns_rate_limited(
    admin_user, manager, totp_svc, rate_limiter_locked
):
    result = _call_elevate(
        {"totp_code": _VALID_TOTP},
        admin_user,
        _SESSION_KEY,
        manager,
        totp_svc,
        rate_limiter_locked,
    )
    assert result.get("error") == "rate_limited"
