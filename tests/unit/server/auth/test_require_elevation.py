"""
Tests for require_elevation FastAPI dependency (Story #923 AC5).

Verifies that:
- Passthrough (return user) when kill switch is off (elevation_enforcement_enabled=False).
- Passthrough (return user) when elevated_session_manager is None.
- HTTP 403 totp_setup_required is raised when admin has no TOTP MFA enabled.
- HTTP 403 elevation_required is raised when no session key is present.
- HTTP 403 elevation_required is raised when no elevation window exists.
- HTTP 403 elevation_required is raised when elevation window has been revoked.
- Passes when an active elevation window exists with matching scope.
- Cookie-based session key resolution works when no JTI is present.
- Recovery-scope window raises 403 when required_scope='full'.
- Recovery-scope window passes when required_scope='totp_repair'.
- Full-scope window passes when required_scope='full'.

NOTE (Bug #AT-12): The original tests asserted HTTP 503 for the two kill-switch-off
cases.  That was a misinterpretation of the kill-switch semantics.  The correct
behavior is passthrough (return the user unchanged) so protected endpoints operate
without elevation when the feature is administratively disabled.  The 503 response
was never intended for this path — it applies only to the /auth/elevate endpoint
itself (elevation_routes.py).  Tests have been renamed and updated accordingly.
"""

import contextlib
import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
import code_indexer.server.auth.dependencies as _deps

# Named constants
_SESSION_KEY = "test_session_jti_abc"
_COOKIE_KEY = "test_cookie_session_xyz"
_USERNAME = "admin"
_IP = "127.0.0.1"
_IDLE_TIMEOUT = 300
_MAX_AGE = 1800
_HTTP_403 = 403

# Patch targets
_ENFORCEMENT_ENABLED_PATH = (
    "code_indexer.server.auth.dependencies._is_elevation_enforcement_enabled"
)
_TOTP_IS_MFA_ENABLED_PATH = (
    "code_indexer.server.auth.totp_service.TOTPService.is_mfa_enabled"
)
_GET_TOTP_SERVICE_PATH = "code_indexer.server.web.mfa_routes.get_totp_service"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Real ElevatedSessionManager for testing."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_TIMEOUT,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elevated_sessions.db"),
    )


@pytest.fixture
def user():
    """Real User object with admin role."""
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


# ---------------------------------------------------------------------------
# Scaffolding helpers — eliminate repeated patch/setup across tests
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _elevation_ctx(enforcement: bool = True, mfa_enabled: bool = True):
    """Patch kill-switch, totp service singleton, and is_mfa_enabled together.

    Production now calls get_totp_service() (returns None outside lifespan),
    so we stub it to return a MagicMock. Class-level TOTPService.is_mfa_enabled
    patch does not apply to MagicMock instances, so we configure the MagicMock's
    is_mfa_enabled directly.
    """
    fake_totp_service = MagicMock()
    fake_totp_service.is_mfa_enabled.return_value = mfa_enabled
    with (
        patch(_ENFORCEMENT_ENABLED_PATH, return_value=enforcement),
        patch(_GET_TOTP_SERVICE_PATH, return_value=fake_totp_service),
    ):
        yield


def _make_request(jti) -> MagicMock:
    """Build a mock FastAPI Request whose JWT payload has the given jti."""
    request = MagicMock()
    request.state.user_jti = jti
    request.cookies = {}
    return request


def _make_cookie_request(cookie_value: str) -> MagicMock:
    """Build a mock FastAPI Request with no JTI but a cidx_session cookie."""
    request = MagicMock()
    request.state.user_jti = None
    request.cookies = {"cidx_session": cookie_value}
    return request


def _run_dep(
    request, user, manager, required_scope: str = "full", mfa_enabled: bool = True
):
    """Set manager, patch enforcement+TOTP, build dependency, call it, return result.

    Centralises the common setup for non-kill-switch, non-manager-None tests.
    Kill-switch and manager-None tests call _elevation_ctx directly since they
    test conditions outside _run_dep's enforcement=True assumption.
    """
    _deps.elevated_session_manager = manager
    with _elevation_ctx(enforcement=True, mfa_enabled=mfa_enabled):
        dep = _deps.require_elevation(required_scope=required_scope)
        return dep(request, user)


# ---------------------------------------------------------------------------
# Kill switch and manager-None cases — passthrough (corrected per Bug #AT-12)
# Prior behavior raised HTTP 503; correct behavior is to return the user so
# protected endpoints run without an elevation gate when the feature is off.
# ---------------------------------------------------------------------------


def test_kill_switch_off_passes_through(manager, user):
    """When kill switch is OFF, dependency must return user without raising.

    Prior (wrong) behavior: raised HTTP 503 elevation_enforcement_disabled.
    Correct behavior: bypass all elevation checks, return user unchanged.
    """
    _deps.elevated_session_manager = manager
    manager.create(_SESSION_KEY, _USERNAME, _IP)
    with _elevation_ctx(enforcement=False):
        dep = _deps.require_elevation()
        result = dep(_make_request(_SESSION_KEY), user)
    assert result is user


def test_manager_none_passes_through(user):
    """When elevated_session_manager is None AND enforcement is ON, pass through.

    Prior (wrong) behavior: raised HTTP 503 when manager was None.
    Correct behavior: None manager means the subsystem is not initialised on
    this deployment — treat equivalently to kill switch OFF and return user.
    """
    _deps.elevated_session_manager = None
    with _elevation_ctx(enforcement=True):
        dep = _deps.require_elevation()
        result = dep(_make_request(_SESSION_KEY), user)
    assert result is user


# ---------------------------------------------------------------------------
# TOTP setup gate (403 totp_setup_required)
# ---------------------------------------------------------------------------


def test_raises_403_totp_setup_required_when_mfa_not_enabled(manager, user):
    """Must raise HTTP 403 totp_setup_required when is_mfa_enabled returns False."""
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user, manager, mfa_enabled=False)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "totp_setup_required"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here
    assert exc_info.value.detail["setup_url"] == "/admin/mfa/setup"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here


# ---------------------------------------------------------------------------
# Session key resolution and window cases (403 elevation_required)
# ---------------------------------------------------------------------------


def test_raises_403_when_no_session_key_present(manager, user):
    """Must raise HTTP 403 when both JTI and cidx_session cookie are absent."""
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(None), user, manager)  # no JTI, no cookie
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here


def test_raises_403_when_no_elevation_window(manager, user):
    """Must raise HTTP 403 when no elevation window exists for the session key."""
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user, manager)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here


def test_raises_403_when_elevation_window_revoked(manager, user):
    """Must raise HTTP 403 when touch_atomic returns None (window revoked)."""
    manager.create(_SESSION_KEY, _USERNAME, _IP)
    manager.revoke(_SESSION_KEY)
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user, manager)
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here


# ---------------------------------------------------------------------------
# Happy path — active window passes
# ---------------------------------------------------------------------------


def test_passes_when_elevation_window_active(manager, user):
    """Must not raise when an active elevation window exists."""
    manager.create(_SESSION_KEY, _USERNAME, _IP)
    result = _run_dep(_make_request(_SESSION_KEY), user, manager)
    assert result is user


# ---------------------------------------------------------------------------
# Cookie-based session key resolution
# ---------------------------------------------------------------------------


def test_cookie_session_key_resolves_when_no_jti(manager, user):
    """Must resolve session key from cidx_session cookie when JTI is absent."""
    manager.create(_COOKIE_KEY, _USERNAME, _IP)
    result = _run_dep(_make_cookie_request(_COOKIE_KEY), user, manager)
    assert result is user


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


def test_raises_403_when_recovery_scope_window_and_full_required(manager, user):
    """Recovery-scope window must not satisfy required_scope='full'."""
    manager.create(_SESSION_KEY, _USERNAME, _IP, scope="totp_repair")
    with pytest.raises(HTTPException) as exc_info:
        _run_dep(_make_request(_SESSION_KEY), user, manager, required_scope="full")
    assert exc_info.value.status_code == _HTTP_403
    assert exc_info.value.detail["error"] == "elevation_required"  # type: ignore[index]  # HTTPException.detail is str|Any; dict access is safe here


def test_passes_when_recovery_scope_window_and_totp_repair_required(manager, user):
    """Recovery-scope window must satisfy required_scope='totp_repair'."""
    manager.create(_SESSION_KEY, _USERNAME, _IP, scope="totp_repair")
    result = _run_dep(
        _make_request(_SESSION_KEY), user, manager, required_scope="totp_repair"
    )
    assert result is user


def test_passes_when_full_scope_window_and_full_required(manager, user):
    """Full-scope window must satisfy required_scope='full'."""
    manager.create(_SESSION_KEY, _USERNAME, _IP, scope="full")
    result = _run_dep(_make_request(_SESSION_KEY), user, manager, required_scope="full")
    assert result is user
