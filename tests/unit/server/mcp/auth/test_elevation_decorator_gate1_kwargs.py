"""Tests for require_mcp_elevation decorator session_key pop behavior.

Verifies that session_key is always popped from kwargs BEFORE any gate fires,
so handlers that do not declare session_key never receive a TypeError regardless
of which gate exits first.

Bug A: When enforcement is disabled (Gate 1 kill switch), the original code
called handler(args, user, *extra, **kwargs) with session_key still present in
kwargs, causing TypeError for handlers that do not declare session_key.

Fix: Pop session_key unconditionally before Gate 1 so ALL gate exits pass
clean kwargs to the handler.
"""

import contextlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
from code_indexer.server.mcp.auth.elevation_decorator import require_mcp_elevation

# ---------------------------------------------------------------------------
# Patch targets
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
_SESSION_KEY = "jti-test-gate1-kwargs-001"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800


# ---------------------------------------------------------------------------
# Contextmanager helper
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
        db_path=str(tmp_path / "elev_gate1_kwargs.db"),
    )


@pytest.fixture
def totp_enabled():
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


# ---------------------------------------------------------------------------
# Handler that captures kwargs so we can assert session_key is absent.
# Also works as the handler that has no session_key param — if session_key
# were leaked into kwargs, Python would raise TypeError.
# ---------------------------------------------------------------------------


def _handler_captures_kwargs(args, user, **kwargs):
    """Handler that captures kwargs so we can assert session_key is absent."""
    return {"success": True, "called": True, "got_kwargs": dict(kwargs)}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_gate1_disabled_no_session_key_in_handler_kwargs(
    admin_user, manager, totp_enabled
):
    """When enforcement is disabled (Gate 1 kill switch fires), session_key must
    be absent from kwargs passed to the handler, even when session_key was
    supplied to the wrapper.

    This is the core Bug A regression test: before the fix, session_key remained
    in kwargs and a handler without that parameter raised TypeError, which
    propagated as JSON-RPC -32603 instead of the expected payload.
    """
    decorated = require_mcp_elevation()(_handler_captures_kwargs)

    with _patch_all(manager, totp_enabled, enforcement=False):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)

    assert result.get("called") is True, (
        f"Handler must be called when enforcement is disabled, got: {result}"
    )
    got_kwargs = result.get("got_kwargs", {})
    assert "session_key" not in got_kwargs, (
        f"session_key must be popped before Gate 1 fires, "
        f"but it leaked into handler kwargs: {got_kwargs}"
    )


def test_gate1_enabled_session_key_not_in_handler_kwargs(
    admin_user, manager, totp_enabled
):
    """When enforcement is enabled and the elevation window exists (all gates
    pass), session_key must also be absent from kwargs passed to the handler.
    """
    # Create a real elevation window so Gate 6 passes
    manager.create(
        session_key=_SESSION_KEY,
        username=_USERNAME,
        elevated_from_ip="127.0.0.1",
        scope="full",
    )
    decorated = require_mcp_elevation()(_handler_captures_kwargs)

    with _patch_all(manager, totp_enabled, enforcement=True):
        result = decorated({}, admin_user, session_key=_SESSION_KEY)

    assert result.get("called") is True, (
        f"Handler must be called when elevation window exists, got: {result}"
    )
    got_kwargs = result.get("got_kwargs", {})
    assert "session_key" not in got_kwargs, (
        f"session_key must not appear in handler kwargs, got: {got_kwargs}"
    )


def test_gate1_enabled_no_session_key_returns_elevation_required(
    admin_user, manager, totp_enabled
):
    """When enforcement is enabled and no session_key is provided, the decorator
    must return an elevation_required error — handler must NOT be invoked.
    """
    decorated = require_mcp_elevation()(_handler_captures_kwargs)

    with _patch_all(manager, totp_enabled, enforcement=True):
        result = decorated({}, admin_user)  # no session_key at all

    assert result.get("error") == "elevation_required", (
        f"Expected elevation_required when no session key supplied, got: {result}"
    )
    assert result.get("called") is None, (
        "Handler must NOT be invoked when elevation window is missing"
    )
