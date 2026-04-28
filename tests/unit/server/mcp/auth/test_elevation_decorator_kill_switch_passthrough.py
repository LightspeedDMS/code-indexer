"""Tests for require_mcp_elevation decorator kill-switch passthrough behavior.

When elevation enforcement is disabled (kill switch off), the decorator must
pass through to the wrapped handler and return its value — NOT return the
elevation_enforcement_disabled error dict.

Per user policy: 'if no TOTP elevation is enabled, you must passthru'.

Patch targets follow the exact established pattern in test_elevation_decorator.py:
patch at module-level import seams (not internal helpers).
"""

import contextlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Patch targets (same seams as test_elevation_decorator.py)
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_USERNAME = "admin"
_SESSION_KEY = "jti-test-passthrough-dec-001"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800


# ---------------------------------------------------------------------------
# Contextmanager helper (mirrors test_elevation_decorator.py)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_all(esm, totp_svc, enforcement=True):
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
        db_path=str(tmp_path / "elev_passthrough.db"),
    )


@pytest.fixture
def totp_enabled():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


@pytest.fixture
def decorated():
    """Pre-wrapped stub handler with default require_mcp_elevation()."""
    return require_mcp_elevation()(_stub_handler)


# ---------------------------------------------------------------------------
# Shared stub handler
# ---------------------------------------------------------------------------


def _stub_handler(args, user, session_key=None):
    """Minimal handler that records it was called."""
    return {"success": True, "called": True}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_decorator_when_disabled_passes_through_to_handler(
    decorated, admin_user, manager, totp_enabled
):
    """Kill switch OFF -> decorator invokes handler and returns its value.

    Per user policy: when elevation enforcement is disabled, the decorator
    must NOT return elevation_enforcement_disabled — it must passthru.
    """
    with _patch_all(manager, totp_enabled, enforcement=False):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)

    assert result.get("called") is True, (
        f"Expected handler to be called (called=True), got: {result}"
    )
    assert result.get("success") is True
    assert "error" not in result, (
        f"Expected no error key when kill switch is off, got: {result}"
    )


def test_decorator_when_enabled_no_session_returns_elevation_required(
    decorated, admin_user, manager, totp_enabled
):
    """Kill switch ON + no session -> elevation_required, handler NOT invoked.

    Confirms the enforcement-on path is unchanged after adding the passthru.
    Handler must not be called when elevation is missing.
    """
    # manager has no active window for _SESSION_KEY
    with _patch_all(manager, totp_enabled, enforcement=True):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)

    assert result["error"] == "elevation_required", (
        f"Expected elevation_required error, got: {result}"
    )
    assert result.get("called") is None, (
        "Handler must NOT be invoked when elevation window is missing"
    )
