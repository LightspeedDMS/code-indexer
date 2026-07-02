"""
Tests for the corrected kill-switch-off semantics of require_elevation (Bug #AT-12).

When elevation_enforcement_enabled=False (kill switch OFF), the dependency MUST
pass through and return the user unchanged.  It must NOT raise HTTP 503.

Correct semantics:
  - enforcement OFF → bypass all elevation checks → return user (200)
  - enforcement OFF + manager None → same bypass → return user (200)
  - enforcement ON + no active window → raise 403 elevation_required

The old code raised 503 in both OFF cases — that was the show-stopper bug.
"""

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
import code_indexer.server.auth.dependencies as _deps

# Named constants
_SESSION_KEY = "test_session_jti_passthrough_abc"
_USERNAME = "admin"
_IP = "127.0.0.1"
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800
_HTTP_403 = 403

# Patch targets
_ENFORCEMENT_ENABLED_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Real ElevatedSessionManager for tests that need a real manager."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_TIMEOUT,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elevated_sessions_passthrough.db"),
    )


@pytest.fixture
def user():
    """Real admin User object."""
    from datetime import datetime, timezone
    from code_indexer.server.auth.password_manager import PasswordManager

    return User(
        username=_USERNAME,
        role="admin",
        password_hash=PasswordManager().hash_password("testpassword"),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _restore_manager():
    """Restore module-level elevated_session_manager after each test."""
    original = getattr(_deps, "elevated_session_manager", None)
    yield
    _deps.elevated_session_manager = original


def _make_request(jti=_SESSION_KEY) -> MagicMock:
    request = MagicMock()
    request.state.user_jti = jti
    request.cookies = {}
    return request


# ---------------------------------------------------------------------------
# Kill-switch OFF → passthrough (the corrected behavior)
# ---------------------------------------------------------------------------


def test_disabled_enforcement_bypasses_check(manager, user):
    """When enforcement is OFF, dependency returns user without raising.

    Previously raised HTTP 503 (the show-stopper bug).  Correct behavior:
    bypass all elevation checks and return the user unchanged.
    """
    _deps.elevated_session_manager = manager
    # No elevation window created — would fail if checks ran
    with patch(_ENFORCEMENT_ENABLED_PATH, return_value=False):
        dep = _deps.require_elevation()
        result = dep(_make_request(), user)
    assert result is user


def test_disabled_enforcement_with_no_manager_still_passes(user):
    """When enforcement is OFF and manager is None, dependency returns user.

    The kill-switch being OFF means 'do not enforce elevation' — a None
    manager is irrelevant when enforcement is disabled.  Must NOT raise.
    """
    _deps.elevated_session_manager = None
    with patch(_ENFORCEMENT_ENABLED_PATH, return_value=False):
        dep = _deps.require_elevation()
        result = dep(_make_request(), user)
    assert result is user


# ---------------------------------------------------------------------------
# Kill-switch ON → enforcement path still raises 403 correctly
# ---------------------------------------------------------------------------


def test_enabled_enforcement_no_session_returns_403(manager, user):
    """When enforcement is ON and no elevation window exists, raise 403.

    This confirms that fixing the kill-switch-off case did NOT break the
    enforcement-on path.  403 elevation_required is the correct error.
    """
    _deps.elevated_session_manager = manager
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = True
    with (
        patch(_ENFORCEMENT_ENABLED_PATH, return_value=True),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        dep = _deps.require_elevation()
        with pytest.raises(HTTPException) as exc_info:
            dep(_make_request(), user)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]
