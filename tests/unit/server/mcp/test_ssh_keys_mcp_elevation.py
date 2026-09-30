"""
MCP elevation parity for manage_ssh_key.

The REST route ``POST /{name}/hosts`` (and the create/delete routes) carry
``require_elevation()``; the MCP tool performing the identical mutation must
require elevation too, so an admin JWT with no live TOTP cannot assign a host
via MCP when the REST/web path would be blocked. The MCP tool gates the same
mutating actions
REST gates: create, delete, assign_host. show_public and list_ssh_keys stay
UNGATED (read-only, matching REST's GET /{name}/public which has no
elevation dependency).

Discriminating RED: modeled directly on
tests/unit/server/mcp/test_admin_tools_elevation_required.py's
``_patch_all`` helper (real ElevatedSessionManager with NO window created,
TOTP enabled, enforcement ON) -- this reproduces the exact "admin JWT, no
live TOTP" scenario. Without the
``@require_mcp_elevation`` decorators on ``mcp/handlers/ssh_keys.py``,
these calls would invoke the real manager instead of returning
``elevation_required``.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
import code_indexer.server.mcp.handlers.ssh_keys as ssh_keys_handlers


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


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


@pytest.fixture
def admin_user():
    return User(
        username=_USERNAME,
        role=UserRole.ADMIN,
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


@contextlib.contextmanager
def _patch_all(manager, totp_svc):
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, manager),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        yield


# ---------------------------------------------------------------------------
# Mutating actions must be gated, via the public dispatcher AND directly.
# ---------------------------------------------------------------------------

_GATED_ACTIONS = [
    pytest.param({"action": "create", "name": "deploy-key_1.v2"}, id="create"),
    pytest.param({"action": "delete", "name": "deploy-key_1.v2"}, id="delete"),
    pytest.param(
        {"action": "assign_host", "name": "deploy-key_1.v2", "hostname": "github.com"},
        id="assign_host",
    ),
]


@pytest.mark.parametrize("args", _GATED_ACTIONS)
def test_manage_ssh_key_gated_action_returns_elevation_required(
    args, admin_user, manager, totp_enabled
):
    """Via the public dispatcher handle_manage_ssh_key, exactly as MCP protocol
    dispatch would invoke it: (args, user, session_key=...)."""
    with _patch_all(manager, totp_enabled):
        result = ssh_keys_handlers.handle_manage_ssh_key(
            args, admin_user, session_key=_SESSION_KEY
        )
    parsed = _parse_mcp_response(result)
    assert parsed.get("error") == "elevation_required", (
        f"Expected elevation_required for action={args['action']!r}, got: {result}"
    )


def test_assign_host_inner_handler_is_gated_directly(admin_user, manager, totp_enabled):
    """Inner handler decorated directly (Story #992 pattern: inner handlers
    preserve @require_mcp_elevation() even when called outside the dispatcher)."""
    with _patch_all(manager, totp_enabled):
        result = ssh_keys_handlers._assign_host(
            {"name": "deploy-key_1.v2", "hostname": "github.com"},
            admin_user,
            session_key=_SESSION_KEY,
        )
    parsed = _parse_mcp_response(result)
    assert parsed.get("error") == "elevation_required"


def test_handle_manage_ssh_key_declares_session_key_marker():
    """protocol.py's Case B session_key injection requires this marker on the
    TOP-LEVEL dispatcher, since handle_manage_ssh_key itself does not declare
    a `session_key` parameter (mirrors handle_manage_mcp_credential)."""
    assert (
        getattr(
            ssh_keys_handlers.handle_manage_ssh_key,
            "__mcp_requires_session_key__",
            False,
        )
        is True
    )


# ---------------------------------------------------------------------------
# Read-only actions must stay ungated.
# ---------------------------------------------------------------------------


def test_show_public_action_not_gated(admin_user, manager, totp_enabled):
    mock_manager = MagicMock()
    mock_manager.get_public_key.return_value = "ssh-ed25519 AAAA test"
    with (
        _patch_all(manager, totp_enabled),
        patch(
            "code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager",
            return_value=mock_manager,
        ),
    ):
        result = ssh_keys_handlers.handle_manage_ssh_key(
            {"action": "show_public", "name": "deploy-key_1.v2"},
            admin_user,
            session_key=_SESSION_KEY,
        )
    parsed = _parse_mcp_response(result)
    assert parsed.get("error") != "elevation_required", (
        f"show_public must not be elevation-gated: {result}"
    )


def test_list_ssh_keys_not_gated(admin_user, manager, totp_enabled):
    mock_manager = MagicMock()
    mock_result = MagicMock()
    mock_result.managed = []
    mock_result.unmanaged = []
    mock_manager.list_keys.return_value = mock_result
    with (
        _patch_all(manager, totp_enabled),
        patch(
            "code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager",
            return_value=mock_manager,
        ),
    ):
        result = ssh_keys_handlers.handle_list_ssh_keys({}, admin_user)
    parsed = _parse_mcp_response(result)
    assert parsed.get("error") != "elevation_required", (
        f"list_ssh_keys must not be elevation-gated: {result}"
    )
