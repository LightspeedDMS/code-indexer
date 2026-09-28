"""
A caller naming their OWN username as `target_user` (manage_mcp_credential
create/delete) or `username` (list_mcp_credentials scope='user') is acting
on themselves -- self-service by definition, exactly like omitting
target_user / using scope='self'. The admin-role gate introduced for
managing ANOTHER user's credentials must not apply to this case: before
that gate existed, a non-admin naming themselves this way already worked.

Driven through the real MCP dispatcher (`handle_tools_call`, real
`TOOL_REGISTRY` and `HANDLER_REGISTRY` entries), mirroring the established
harness pattern for these two tools.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole
import code_indexer.server.mcp.handlers.admin.mcp_credentials as mcp_credentials_handlers

pytestmark = pytest.mark.asyncio

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"

_SESSION_ID = "mcp-session-own-name-self-route"
_ELEVATION_KEY = "own-name-self-route-session-key"
_IDLE = 300
_MAX_AGE = 1800
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_NORMAL_USERNAME = "own-name-normal-user"
_OTHER_USERNAME = "own-name-someone-else"
_CASE_VARIANT_USERNAME = "Own-Name-Normal-User"  # differs only by case


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


@pytest.fixture
def normal_user() -> User:
    return User(
        username=_NORMAL_USERNAME,
        password_hash=_DUMMY_HASH,
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def elevation_manager(tmp_path) -> ElevatedSessionManager:
    """Isolated temp-file-backed ElevatedSessionManager -- never the real
    shared ~/.cidx-server/elevated_sessions.db."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elevated_sessions.db"),
    )


@pytest.fixture
def totp_enabled() -> MagicMock:
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


@pytest.fixture
def mock_cred_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get_credentials.return_value = []
    mgr.generate_credential_audited.return_value = {
        "credential_id": "cred-own-name-123",
        "client_id": "mcp_client_own_name",
        "client_secret": "mcp_secret_own_name",
        "name": "",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mgr.revoke_credential_audited.return_value = True
    return mgr


def _open_elevation_window(esm: ElevatedSessionManager, username: str) -> None:
    esm.create(_ELEVATION_KEY, username, None, scope="full")


@asynccontextmanager
async def _real_dispatch(
    enforcement_enabled: bool,
    esm: ElevatedSessionManager,
    totp_svc: MagicMock,
    cred_manager: Any,
) -> AsyncIterator[Any]:
    """Yield the real handle_tools_call, wired to the real HANDLER_REGISTRY /
    TOOL_REGISTRY entries for both credential tools, with only the
    surrounding request-plumbing (session registry, repo-access guard,
    telemetry) and the credential store stubbed out."""
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY as _REAL_HANDLERS
    from code_indexer.server.mcp.tools import TOOL_REGISTRY as _REAL_TOOLS
    from code_indexer.server.mcp.protocol import handle_tools_call

    real_handlers = {
        "list_mcp_credentials": _REAL_HANDLERS["list_mcp_credentials"],
        "manage_mcp_credential": _REAL_HANDLERS["manage_mcp_credential"],
    }
    real_tools = {
        "list_mcp_credentials": _REAL_TOOLS["list_mcp_credentials"],
        "manage_mcp_credential": _REAL_TOOLS["manage_mcp_credential"],
    }

    mock_session_state = MagicMock()
    mock_session_state.is_impersonating = False

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            real_handlers,
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            real_tools,
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.session_registry.get_session_registry"
        ) as mock_registry,
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        patch(_ENFORCEMENT_PATH, return_value=enforcement_enabled),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
        patch.object(mcp_credentials_handlers, "dependencies") as mock_deps,
    ):
        mock_registry.return_value.get_or_create_session.return_value = (
            mock_session_state
        )
        mock_deps.mcp_credential_manager = cred_manager
        yield handle_tools_call


async def _dispatch(
    handle_tools_call: Any,
    tool_name: str,
    arguments: Dict[str, Any],
    user: User,
) -> dict:
    return await handle_tools_call(  # type: ignore[no-any-return]
        params={"name": tool_name, "arguments": dict(arguments)},
        user=user,
        session_id=_SESSION_ID,
        elevation_key=_ELEVATION_KEY,
    )


# ---------------------------------------------------------------------------
# Own name (via target_user / username) routes to the self path.
# ---------------------------------------------------------------------------


class TestOwnNameRoutesToSelf:
    async def test_create_with_target_user_equal_to_caller_succeeds(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "create", "target_user": normal_user.username},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True, content
        mock_cred_manager.generate_credential_audited.assert_called_once_with(
            normal_user.username, "", actor=normal_user.username
        )

    async def test_delete_with_target_user_equal_to_caller_succeeds(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {
                    "action": "delete",
                    "target_user": normal_user.username,
                    "credential_id": "cred-1",
                },
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True, content
        mock_cred_manager.revoke_credential_audited.assert_called_once_with(
            normal_user.username, "cred-1", actor=normal_user.username
        )

    async def test_list_scope_user_with_username_equal_to_caller_succeeds(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        """Routes to _list_self, which (like scope='self') never required
        elevation -- no window is opened for this case."""
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "list_mcp_credentials",
                {"scope": "user", "username": normal_user.username},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True, content
        mock_cred_manager.get_credentials.assert_called_once_with(normal_user.username)


# ---------------------------------------------------------------------------
# Any OTHER name still requires the admin role -- the fix must not loosen
# the invariant 042 introduced for a genuinely different target.
# ---------------------------------------------------------------------------


class TestOtherNameStillRequiresAdminRole:
    async def test_create_with_a_different_target_user_is_denied(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "create", "target_user": _OTHER_USERNAME},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is False
        assert content["error"] == mcp_credentials_handlers._ADMIN_ROLE_REQUIRED_ERROR
        mock_cred_manager.generate_credential_audited.assert_not_called()

    async def test_delete_with_a_different_target_user_is_denied(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {
                    "action": "delete",
                    "target_user": _OTHER_USERNAME,
                    "credential_id": "cred-1",
                },
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is False
        assert content["error"] == mcp_credentials_handlers._ADMIN_ROLE_REQUIRED_ERROR
        mock_cred_manager.revoke_credential_audited.assert_not_called()

    async def test_list_scope_user_with_a_different_username_is_denied(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "list_mcp_credentials",
                {"scope": "user", "username": _OTHER_USERNAME},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is False
        assert content["error"] == mcp_credentials_handlers._ADMIN_ROLE_REQUIRED_ERROR
        mock_cred_manager.get_credentials.assert_not_called()


# ---------------------------------------------------------------------------
# A case-variant of the caller's own username is a DIFFERENT username under
# this codebase's own comparison convention (plain ==, never case-folded --
# see mfa_routes.py's target_user != admin_username) -- must still be
# denied, never treated as self.
# ---------------------------------------------------------------------------


class TestCaseVariantIsADifferentUser:
    async def test_create_with_a_case_variant_of_the_callers_name_is_denied(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        assert _CASE_VARIANT_USERNAME != normal_user.username
        assert _CASE_VARIANT_USERNAME.lower() == normal_user.username.lower()

        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "create", "target_user": _CASE_VARIANT_USERNAME},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is False
        assert content["error"] == mcp_credentials_handlers._ADMIN_ROLE_REQUIRED_ERROR
        mock_cred_manager.generate_credential_audited.assert_not_called()
