"""Tests for elevation decorator security fixes.

AC6: require_mcp_elevation() wrapper has __mcp_requires_session_key__ = True attribute.
AC7: Cross-user bypass is denied — when Admin A's elevation window exists for
     session_key=K, calling an elevation-gated handler with session_key=K but
     user=Admin_B returns elevation_required error.
AC8: Same session_key + correct owner -> handler is called successfully
     (validates the bypass fix doesn't break the happy path).
"""

import contextlib
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_USERNAME_A = "admin_a"
_USERNAME_B = "admin_b"
_SESSION_KEY = "shared-session-key-xyz"
_CLIENT_IP = "127.0.0.1"
_FAKE_PW_HASH = "not-a-real-hash-sentinel-for-tests"
_IDLE = 300
_MAX_AGE = 1800

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"


def _make_user(username: str) -> User:
    return User(
        username=username,
        role="admin",
        password_hash=_FAKE_PW_HASH,
        created_at=datetime.now(timezone.utc),
    )


def _make_totp_enabled() -> MagicMock:
    """Return a mock TOTP service that reports MFA enabled for any username."""
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


@contextlib.contextmanager
def _patch_all(esm, totp_svc, enforcement=True):
    with (
        patch(_ENFORCEMENT_PATH, return_value=enforcement),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield


def _noop_handler(args, user, session_key=None):
    return {"success": True, "called": True}


# ---------------------------------------------------------------------------
# AC6: wrapper has __mcp_requires_session_key__ = True attribute
# ---------------------------------------------------------------------------


def test_ac6_wrapper_has_mcp_requires_session_key_marker():
    """require_mcp_elevation() wrapper must carry __mcp_requires_session_key__ = True.

    This explicit marker replaces the heuristic VAR_KEYWORD detection in
    _invoke_handler. Protocol code reads this attribute to decide whether to
    inject session_key via **kwargs.
    """
    decorated = require_mcp_elevation()(_noop_handler)

    assert hasattr(decorated, "__mcp_requires_session_key__"), (
        "require_mcp_elevation wrapper must expose __mcp_requires_session_key__ attribute"
    )
    assert decorated.__mcp_requires_session_key__ is True, (
        "__mcp_requires_session_key__ must be True, got "
        f"{decorated.__mcp_requires_session_key__!r}"
    )


# ---------------------------------------------------------------------------
# AC7: cross-user bypass is denied
# ---------------------------------------------------------------------------


def test_ac7_cross_user_bypass_is_denied(tmp_path):
    """Admin B cannot use Admin A's elevation window via a known session_key.

    Setup: Admin A's elevation window exists for session_key=K.
    Action: Call the gated handler with session_key=K but user=Admin_B.
    Expected: elevation_required error (NOT handler invocation).
    """
    manager = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "cross_user.db"),
    )
    manager.create(_SESSION_KEY, _USERNAME_A, _CLIENT_IP, scope="full")

    decorated = require_mcp_elevation()(_noop_handler)
    user_b = _make_user(_USERNAME_B)

    with _patch_all(manager, _make_totp_enabled(), enforcement=True):
        result = decorated({}, user_b, session_key=_SESSION_KEY)

    assert result.get("error") == "elevation_required", (
        "Admin B must not be able to use Admin A's session window. "
        f"Got result: {result!r}. Cross-user bypass is still open."
    )
    assert result.get("success") is not True, (
        "Handler must NOT be called when cross-user bypass is attempted"
    )


# ---------------------------------------------------------------------------
# AC8: correct owner succeeds (happy path not broken by the fix)
# ---------------------------------------------------------------------------


def test_ac8_correct_owner_succeeds(tmp_path):
    """Admin A using their own session_key passes through to the handler.

    Validates that the fix (using touch_atomic_for_user) does not break
    the legitimate elevation flow.
    """
    manager = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "happy_path.db"),
    )
    manager.create(_SESSION_KEY, _USERNAME_A, _CLIENT_IP, scope="full")

    decorated = require_mcp_elevation()(_noop_handler)
    user_a = _make_user(_USERNAME_A)

    with _patch_all(manager, _make_totp_enabled(), enforcement=True):
        result = decorated({}, user_a, session_key=_SESSION_KEY)

    assert result.get("success") is True, (
        "Admin A using their own session_key must reach the handler. "
        f"Got result: {result!r}."
    )
    assert result.get("called") is True
