"""
Tests for AC2: canonical sensitive MCP tools gated by @require_mcp_elevation.
(Story #925 AC2)

3 test functions:
1. Parametrized over all 9 canonical gated tools: each returns exactly
   elevation_required when enforcement is ON, TOTP enabled, but no window.
2. list_users (non-gated) is NOT blocked.
3. handle_get_global_config (non-gated read) is NOT blocked.

_patch_all contextmanager applies enforcement=True + real manager (no window)
+ TOTP enabled, which triggers the exact elevation_required path.
"""

import contextlib
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User
import code_indexer.server.mcp.handlers.admin as admin_handlers

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------
_USERNAME = "admin"
_SESSION_KEY = "no-window-session-key"
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_IDLE = 300
_MAX_AGE = 1800

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"


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


# ---------------------------------------------------------------------------
# Contextmanager helper: enforcement ON, real manager with NO window, TOTP on
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_all(manager, totp_svc):
    """Apply enforcement=True, real ESM (no window created), TOTP enabled."""
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, manager),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield


# ---------------------------------------------------------------------------
# 9 canonical gated handlers — parametrized to eliminate duplication
# ---------------------------------------------------------------------------

_GATED_HANDLERS = [
    pytest.param(
        lambda h: admin_handlers.create_user({}, h, session_key=_SESSION_KEY),
        id="create_user",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_set_session_impersonation(
            {}, h, session_key=_SESSION_KEY
        ),
        id="set_session_impersonation",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_set_global_config(
            {}, h, session_key=_SESSION_KEY
        ),
        id="set_global_config",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_create_group({}, h, session_key=_SESSION_KEY),
        id="create_group",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_delete_group({}, h, session_key=_SESSION_KEY),
        id="delete_group",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_add_member_to_group(
            {}, h, session_key=_SESSION_KEY
        ),
        id="add_member_to_group",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_remove_member_from_group(
            {}, h, session_key=_SESSION_KEY
        ),
        id="remove_member_from_group",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_admin_create_user_mcp_credential(
            {}, h, session_key=_SESSION_KEY
        ),
        id="admin_create_user_mcp_credential",
    ),
    pytest.param(
        lambda h: admin_handlers.handle_delete_api_key({}, h, session_key=_SESSION_KEY),
        id="delete_api_key",
    ),
]


@pytest.mark.parametrize("invoke", _GATED_HANDLERS)
def test_gated_tool_returns_elevation_required(
    invoke, admin_user, manager, totp_enabled
):
    """Each canonical gated tool returns exactly elevation_required when no window."""
    with _patch_all(manager, totp_enabled):
        result = invoke(admin_user)
    assert result.get("error") == "elevation_required", (
        f"Expected elevation_required, got: {result}"
    )


# ---------------------------------------------------------------------------
# Test: list_users (non-gated) is NOT blocked by elevation
# ---------------------------------------------------------------------------


def test_list_users_not_gated(admin_user, manager, totp_enabled):
    """list_users is not elevation-gated and must not return elevation_required."""
    mock_um = MagicMock()
    mock_um.get_all_users.return_value = []
    with (
        _patch_all(manager, totp_enabled),
        patch(
            "code_indexer.server.mcp.handlers._utils.app_module.user_manager", mock_um
        ),
    ):
        result = admin_handlers.list_users({}, admin_user)
    assert result.get("error") != "elevation_required", (
        f"list_users should not be elevation-gated: {result}"
    )


# ---------------------------------------------------------------------------
# Test: handle_get_global_config (non-gated read) is NOT blocked
# ---------------------------------------------------------------------------


def test_get_global_config_not_gated(admin_user, manager, totp_enabled, tmp_path):
    """handle_get_global_config is not elevation-gated.

    Patches _get_golden_repos_dir (filesystem dependency) and
    GlobalRepoOperations (external collaborator) to avoid full server setup.
    """
    mock_ops = MagicMock()
    mock_ops.get_config.return_value = {}
    with (
        _patch_all(manager, totp_enabled),
        patch(
            "code_indexer.server.mcp.handlers.admin._get_golden_repos_dir",
            return_value=str(tmp_path),
        ),
        patch(
            "code_indexer.global_repos.shared_operations.GlobalRepoOperations",
            return_value=mock_ops,
        ),
    ):
        result = admin_handlers.handle_get_global_config({}, admin_user)
    assert result.get("error") != "elevation_required", (
        f"get_global_config should not be elevation-gated: {result}"
    )
