"""
Managing another user's or all users' MCP credentials requires the admin
role.

`list_mcp_credentials` (scope='user'/'all') and `manage_mcp_credential`
(action='create'/'delete' with `target_user`) enforce this with an
admin-role check in the handler, in addition to `@require_mcp_elevation()`
and the tool's `required_permission` ('query_repos', which every role
holds). Self-service on the caller's own credentials (scope='self',
action='create'/'delete' without `target_user`) is available to every
role, and admin behaviour (managing another user's credentials with an
elevation window) is unaffected.

Driven through the real MCP dispatcher (`handle_tools_call`, real
`TOOL_REGISTRY` and `HANDLER_REGISTRY` entries for both tools).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager
from code_indexer.server.auth.user_manager import User, UserManager, UserRole
from code_indexer.server.storage.database_manager import DatabaseSchema
import code_indexer.server.mcp.handlers.admin.mcp_credentials as mcp_credentials_handlers

pytestmark = pytest.mark.asyncio

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
_DEPS_PATH = "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
_UTILS_PATH = "code_indexer.server.mcp.handlers.admin.mcp_credentials._utils"

_SESSION_ID = "mcp-session-role-scoping"
_ELEVATION_KEY = "role-scoping-session-key"
_IDLE = 300
_MAX_AGE = 1800
_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_ADMIN_USERNAME = "role-scoping-admin"
_NORMAL_USERNAME = "role-scoping-normal-user"
_REAL_STORE_PASSWORD = "RealStorePass123!@#"


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def normal_user() -> User:
    return User(
        username=_NORMAL_USERNAME,
        password_hash=_DUMMY_HASH,
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def admin_user() -> User:
    return User(
        username=_ADMIN_USERNAME,
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
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
    """An isolated, in-memory credential store double. Call-log assertions
    on it stand in for "the store is unchanged" -- no persistence backend,
    real or file-backed, is ever touched by this module."""
    mgr = MagicMock()
    mgr.get_credentials.return_value = []
    mgr.generate_credential_audited.return_value = {
        "credential_id": "cred-should-never-exist",
        "client_id": "mcp_should_never_exist",
        "client_secret": "mcp_sec_should_never_exist",
        "name": "",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mgr.revoke_credential_audited.return_value = True
    return mgr


@pytest.fixture
def mock_user_manager() -> MagicMock:
    mgr = MagicMock()
    admin = MagicMock()
    admin.username = _ADMIN_USERNAME
    mgr.get_all_users.return_value = [admin]
    return mgr


@pytest.fixture
def real_credential_store(tmp_path):
    """A real SQLite-backed UserManager + MCPCredentialManager pair in an
    isolated temp directory -- never the real shared ~/.cidx-server
    database. Returns (user_manager, mcp_credential_manager) with the
    admin and normal-user accounts already created."""
    db_path = str(tmp_path / "users.db")
    DatabaseSchema(db_path).initialize_database()
    user_manager = UserManager(use_sqlite=True, db_path=db_path)
    user_manager.create_user(_ADMIN_USERNAME, _REAL_STORE_PASSWORD, UserRole.ADMIN)
    user_manager.create_user(
        _NORMAL_USERNAME, _REAL_STORE_PASSWORD, UserRole.NORMAL_USER
    )
    return user_manager, MCPCredentialManager(user_manager=user_manager)


# ---------------------------------------------------------------------------
# Real-dispatcher harness: handle_tools_call with the real TOOL_REGISTRY /
# HANDLER_REGISTRY entries for list_mcp_credentials and manage_mcp_credential.
# ---------------------------------------------------------------------------


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
        patch(_DEPS_PATH) as mock_deps,
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


def _open_elevation_window(esm: ElevatedSessionManager, username: str) -> None:
    esm.create(_ELEVATION_KEY, username, None, scope="full")


# ---------------------------------------------------------------------------
# A NORMAL_USER acting on another user's (or all users') credentials is
# denied, both with elevation enforcement off and on with the caller's OWN
# elevation window: this is a role gate, distinct from the elevation gate
# the caller may already satisfy.
# ---------------------------------------------------------------------------

_DENIAL_CASES = [
    pytest.param(
        False,
        False,
        "manage_mcp_credential",
        {"action": "create", "target_user": _ADMIN_USERNAME},
        "generate_credential_audited",
        id="create-admin-target-enforcement-off",
    ),
    pytest.param(
        False,
        False,
        "manage_mcp_credential",
        {"action": "create", "target_user": "some-other-user"},
        "generate_credential_audited",
        id="create-other-target-enforcement-off",
    ),
    pytest.param(
        False,
        False,
        "manage_mcp_credential",
        {
            "action": "delete",
            "target_user": _ADMIN_USERNAME,
            "credential_id": "cred-1",
        },
        "revoke_credential_audited",
        id="delete-admin-target-enforcement-off",
    ),
    pytest.param(
        False,
        False,
        "list_mcp_credentials",
        {"scope": "user", "username": _ADMIN_USERNAME},
        "get_credentials",
        id="list-user-admin-target-enforcement-off",
    ),
    pytest.param(
        False,
        False,
        "list_mcp_credentials",
        {"scope": "all"},
        "get_credentials",
        id="list-all-enforcement-off",
    ),
    pytest.param(
        True,
        True,
        "manage_mcp_credential",
        {"action": "create", "target_user": _ADMIN_USERNAME},
        "generate_credential_audited",
        id="create-admin-target-own-window",
    ),
    pytest.param(
        True,
        True,
        "manage_mcp_credential",
        {
            "action": "delete",
            "target_user": "some-other-user",
            "credential_id": "cred-1",
        },
        "revoke_credential_audited",
        id="delete-other-target-own-window",
    ),
    pytest.param(
        True,
        True,
        "list_mcp_credentials",
        {"scope": "all"},
        "get_credentials",
        id="list-all-own-window",
    ),
]


@pytest.mark.parametrize(
    "enforcement_on,open_own_window,tool,args,forbidden_call", _DENIAL_CASES
)
async def test_cross_user_action_denied_for_normal_user(
    enforcement_on,
    open_own_window,
    tool,
    args,
    forbidden_call,
    normal_user,
    elevation_manager,
    totp_enabled,
    mock_cred_manager,
):
    """A NORMAL_USER's create/delete/list against another user's (or all
    users') credentials is denied regardless of whether elevation
    enforcement is on or off, and regardless of whether the caller has
    satisfied elevation with their OWN window -- elevation authenticates
    the caller, it does not authorize acting on someone else's
    credentials."""
    if open_own_window:
        _open_elevation_window(elevation_manager, normal_user.username)

    async with _real_dispatch(
        enforcement_on, elevation_manager, totp_enabled, mock_cred_manager
    ) as call:
        result = await _dispatch(call, tool, args, normal_user)

    content = _parse_mcp_response(result)
    assert content["success"] is False
    assert content["error"] == mcp_credentials_handlers._ADMIN_ROLE_REQUIRED_ERROR
    getattr(mock_cred_manager, forbidden_call).assert_not_called()


# ---------------------------------------------------------------------------
# Self-service on the caller's own credentials stays available to every role.
# ---------------------------------------------------------------------------


class TestSelfServiceUnaffected:
    async def test_normal_user_lists_own_credentials(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call, "list_mcp_credentials", {"scope": "self"}, normal_user
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True
        mock_cred_manager.get_credentials.assert_called_once_with(normal_user.username)

    async def test_normal_user_creates_own_credential_with_elevation(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call, "manage_mcp_credential", {"action": "create"}, normal_user
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True
        mock_cred_manager.generate_credential_audited.assert_called_once_with(
            normal_user.username, "", actor=normal_user.username
        )

    async def test_normal_user_deletes_own_credential_with_elevation(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "delete", "credential_id": "cred-own-1"},
                normal_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True
        mock_cred_manager.revoke_credential_audited.assert_called_once_with(
            normal_user.username, "cred-own-1", actor=normal_user.username
        )


# ---------------------------------------------------------------------------
# Admin behaviour is unchanged: managing another user's credentials still
# works, gated by elevation.
# ---------------------------------------------------------------------------


class TestAdminBehaviourUnchanged:
    async def test_admin_create_for_target_without_window_requires_elevation(
        self, admin_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "create", "target_user": "some-other-user"},
                admin_user,
            )
        content = _parse_mcp_response(result)
        assert content.get("success") is not True
        assert content["error"] == "elevation_required"
        mock_cred_manager.generate_credential_audited.assert_not_called()

    async def test_admin_create_for_target_with_window_succeeds(
        self, admin_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        _open_elevation_window(elevation_manager, admin_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "create", "target_user": "some-other-user"},
                admin_user,
            )
        content = _parse_mcp_response(result)
        assert content["success"] is True
        mock_cred_manager.generate_credential_audited.assert_called_once_with(
            "some-other-user", "", actor=admin_user.username
        )

    async def test_admin_list_all_with_window_succeeds(
        self,
        admin_user,
        elevation_manager,
        totp_enabled,
        mock_cred_manager,
        mock_user_manager,
    ):
        _open_elevation_window(elevation_manager, admin_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, mock_cred_manager
        ) as call:
            with patch(_UTILS_PATH) as mock_utils:
                mock_utils.app_module.user_manager = mock_user_manager
                result = await _dispatch(
                    call, "list_mcp_credentials", {"scope": "all"}, admin_user
                )
        content = _parse_mcp_response(result)
        assert content["success"] is True


# ---------------------------------------------------------------------------
# Mutation proof: with the role check patched to a no-op, the exact denial
# scenario above succeeds instead -- proving the denial tests discriminate
# on the role check itself, not on unrelated plumbing. This deliberately
# patches the function under test (a mutation-testing seam, not a stand-in
# for a collaborator) to reproduce the code path without the role check.
# ---------------------------------------------------------------------------


class TestMutationProof:
    async def test_role_check_neutralised_lets_normal_user_create_admin_credential(
        self, normal_user, elevation_manager, totp_enabled, mock_cred_manager
    ):
        with patch.object(
            mcp_credentials_handlers, "_require_admin_role", return_value=None
        ):
            async with _real_dispatch(
                False, elevation_manager, totp_enabled, mock_cred_manager
            ) as call:
                result = await _dispatch(
                    call,
                    "manage_mcp_credential",
                    {"action": "create", "target_user": _ADMIN_USERNAME},
                    normal_user,
                )
        content = _parse_mcp_response(result)
        assert content["success"] is True
        mock_cred_manager.generate_credential_audited.assert_called_once_with(
            _ADMIN_USERNAME, "", actor=normal_user.username
        )

    async def test_without_the_role_check_normal_user_lists_all_credentials(
        self,
        normal_user,
        elevation_manager,
        totp_enabled,
        mock_cred_manager,
        mock_user_manager,
    ):
        with patch.object(
            mcp_credentials_handlers, "_require_admin_role", return_value=None
        ):
            async with _real_dispatch(
                False, elevation_manager, totp_enabled, mock_cred_manager
            ) as call:
                with patch(_UTILS_PATH) as mock_utils:
                    mock_utils.app_module.user_manager = mock_user_manager
                    result = await _dispatch(
                        call, "list_mcp_credentials", {"scope": "all"}, normal_user
                    )
        content = _parse_mcp_response(result)
        assert content["success"] is True


# ---------------------------------------------------------------------------
# Real store: the self-service delete path scopes strictly to the caller's
# own credentials, even against a real SQLite-backed UserManager (no mocks).
# ---------------------------------------------------------------------------


class TestSelfDeleteAgainstRealStore:
    async def test_self_delete_with_admin_credential_id_leaves_admin_credential_intact(
        self,
        normal_user,
        elevation_manager,
        totp_enabled,
        real_credential_store,
    ):
        user_manager, cred_manager = real_credential_store
        admin_credential = cred_manager.generate_credential(
            _ADMIN_USERNAME, name="admin-real-credential"
        )
        admin_credential_id = admin_credential["credential_id"]

        _open_elevation_window(elevation_manager, normal_user.username)
        async with _real_dispatch(
            True, elevation_manager, totp_enabled, cred_manager
        ) as call:
            result = await _dispatch(
                call,
                "manage_mcp_credential",
                {"action": "delete", "credential_id": admin_credential_id},
                normal_user,
            )

        content = _parse_mcp_response(result)
        assert content["success"] is False

        remaining = user_manager.get_mcp_credentials(_ADMIN_USERNAME)
        assert any(c["credential_id"] == admin_credential_id for c in remaining), (
            "the admin's credential must still exist in the real store"
        )
