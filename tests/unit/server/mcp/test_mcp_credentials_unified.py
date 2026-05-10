"""
Unit tests for unified MCP credential handlers (Story #989).

Tests cover:
- handle_list_mcp_credentials: scope routing (self/user/all/system), missing params,
  elevation semantics (self=undecorated, others=elevated)
- handle_manage_mcp_credential: action routing (create/delete), target_user routing,
  missing params, elevation semantics

TDD: Written before implementation — all tests FAIL until
  src/code_indexer/server/mcp/handlers/admin/mcp_credentials.py is created.
"""

import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Elevation bypass helpers (same pattern as existing credential tests)
# ---------------------------------------------------------------------------
_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
_TEST_SESSION_KEY = "test-session-unified-cred-abc"
_IDLE_SECONDS = 300
_MAX_AGE_SECONDS = 1800
_DB_FILENAME = "elev_unified_cred.db"
_ELEV_SCOPE = "full"


@contextlib.contextmanager
def _active_elevation(username: str, tmp_path):
    """Open a real elevation window so decorated handlers pass the gate."""
    mgr = ElevatedSessionManager(
        idle_timeout_seconds=_IDLE_SECONDS,
        max_age_seconds=_MAX_AGE_SECONDS,
        db_path=str(tmp_path / _DB_FILENAME),
    )
    mgr.create(_TEST_SESSION_KEY, username, None, scope=_ELEV_SCOPE)
    totp_mock = MagicMock()
    totp_mock.is_mfa_enabled.return_value = True
    with (
        patch(_ENFORCEMENT_PATH, return_value=True),
        patch(_ESM_PATH, mgr),
        patch(_TOTP_PATH, return_value=totp_mock),
    ):
        yield _TEST_SESSION_KEY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def normal_user():
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def admin_user():
    return User(
        username="admin_test",
        password_hash="$2b$12$hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def mock_cred_manager():
    mgr = MagicMock()
    mgr.get_credentials.return_value = [
        {
            "credential_id": "cred-123",
            "name": "Test Credential",
            "created_at": "2024-01-01T00:00:00Z",
        }
    ]
    mgr.generate_credential.return_value = {
        "credential_id": "cred-uuid-123",
        "client_id": "mcp_full_client_id",
        "client_secret": "mcp_sec_full_secret_value",
        "name": "Test",
        "created_at": "2024-01-01T00:00:00Z",
    }
    mgr.revoke_credential.return_value = True
    return mgr


@pytest.fixture
def mock_user_manager():
    mgr = MagicMock()
    user1 = MagicMock()
    user1.username = "user1"
    user2 = MagicMock()
    user2.username = "user2"
    mgr.get_all_users.return_value = [user1, user2]
    mgr.get_system_mcp_credentials.return_value = [
        {
            "credential_id": "sys-1",
            "name": "system-auto",
            "is_system": True,
            "created_at": "2024-01-01T00:00:00Z",
        }
    ]
    return mgr


def _parse_content(result: dict) -> dict:
    """Parse MCP content response to dict."""
    assert "content" in result, f"Expected MCP content response, got: {result}"
    return json.loads(result["content"][0]["text"])  # type: ignore[no-any-return]


# =============================================================================
# Registry existence tests
# =============================================================================


class TestUnifiedHandlerRegistryExistence:
    """Both unified handlers must appear in HANDLER_REGISTRY."""

    def test_list_mcp_credentials_in_handler_registry(self):
        """list_mcp_credentials must be registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "list_mcp_credentials" in HANDLER_REGISTRY

    def test_manage_mcp_credential_in_handler_registry(self):
        """manage_mcp_credential must be registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "manage_mcp_credential" in HANDLER_REGISTRY

    def test_old_create_mcp_credential_removed_from_handler_registry(self):
        """create_mcp_credential must NOT be in HANDLER_REGISTRY after hard-cut."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "create_mcp_credential" not in HANDLER_REGISTRY

    def test_old_delete_mcp_credential_removed_from_handler_registry(self):
        """delete_mcp_credential must NOT be in HANDLER_REGISTRY after hard-cut."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "delete_mcp_credential" not in HANDLER_REGISTRY

    def test_old_admin_list_user_mcp_credentials_removed(self):
        """admin_list_user_mcp_credentials must NOT be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_list_user_mcp_credentials" not in HANDLER_REGISTRY

    def test_old_admin_create_user_mcp_credential_removed(self):
        """admin_create_user_mcp_credential must NOT be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_create_user_mcp_credential" not in HANDLER_REGISTRY

    def test_old_admin_delete_user_mcp_credential_removed(self):
        """admin_delete_user_mcp_credential must NOT be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_delete_user_mcp_credential" not in HANDLER_REGISTRY

    def test_old_admin_list_all_mcp_credentials_removed(self):
        """admin_list_all_mcp_credentials must NOT be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_list_all_mcp_credentials" not in HANDLER_REGISTRY

    def test_old_admin_list_system_mcp_credentials_removed(self):
        """admin_list_system_mcp_credentials must NOT be in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "admin_list_system_mcp_credentials" not in HANDLER_REGISTRY


# =============================================================================
# Tool registry (TOOL_REGISTRY) existence tests
# =============================================================================


class TestUnifiedToolRegistryExistence:
    """Both unified tools must appear in TOOL_REGISTRY with correct schema."""

    def test_list_mcp_credentials_in_tool_registry(self):
        """list_mcp_credentials must be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert "list_mcp_credentials" in TOOL_REGISTRY

    def test_manage_mcp_credential_in_tool_registry(self):
        """manage_mcp_credential must be in TOOL_REGISTRY."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert "manage_mcp_credential" in TOOL_REGISTRY

    def test_list_mcp_credentials_has_scope_param(self):
        """list_mcp_credentials schema must have scope parameter."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        schema = TOOL_REGISTRY["list_mcp_credentials"]["inputSchema"]
        assert "scope" in schema["properties"]

    def test_list_mcp_credentials_scope_is_required(self):
        """list_mcp_credentials scope must be required."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        schema = TOOL_REGISTRY["list_mcp_credentials"]["inputSchema"]
        assert "scope" in schema.get("required", [])

    def test_manage_mcp_credential_has_action_param(self):
        """manage_mcp_credential schema must have action parameter."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        schema = TOOL_REGISTRY["manage_mcp_credential"]["inputSchema"]
        assert "action" in schema["properties"]

    def test_manage_mcp_credential_action_is_required(self):
        """manage_mcp_credential action must be required."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        schema = TOOL_REGISTRY["manage_mcp_credential"]["inputSchema"]
        assert "action" in schema.get("required", [])

    def test_list_mcp_credentials_permission_is_query_repos(self):
        """list_mcp_credentials required_permission is query_repos."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert (
            TOOL_REGISTRY["list_mcp_credentials"]["required_permission"]
            == "query_repos"
        )

    def test_manage_mcp_credential_permission_is_query_repos(self):
        """manage_mcp_credential required_permission is query_repos."""
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        assert (
            TOOL_REGISTRY["manage_mcp_credential"]["required_permission"]
            == "query_repos"
        )


# =============================================================================
# list_mcp_credentials: missing scope parameter
# =============================================================================


class TestListMCPCredentialsMissingScope:
    """list_mcp_credentials returns clear error when scope is missing."""

    def test_missing_scope_returns_error(self, normal_user):
        """list_mcp_credentials({}) returns success=False with clear error."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        result = handler({}, normal_user)
        content = _parse_content(result)

        assert content["success"] is False
        assert "scope" in content.get("error", "").lower()

    def test_unknown_scope_returns_error(self, normal_user):
        """list_mcp_credentials with unknown scope returns success=False."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        result = handler({"scope": "bogus"}, normal_user)
        content = _parse_content(result)

        assert content["success"] is False


# =============================================================================
# list_mcp_credentials: scope='self' — no elevation required
# =============================================================================


class TestListMCPCredentialsSelfScope:
    """scope='self' lists caller's own credentials without elevation."""

    def test_self_scope_returns_success_without_elevation(
        self, normal_user, mock_cred_manager
    ):
        """scope='self' succeeds without any elevation window."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with patch(
            "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
        ) as mock_deps:
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler({"scope": "self"}, normal_user)

        content = _parse_content(result)
        assert content["success"] is True

    def test_self_scope_returns_credentials_array(self, normal_user, mock_cred_manager):
        """scope='self' returns credentials array."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with patch(
            "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
        ) as mock_deps:
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler({"scope": "self"}, normal_user)

        content = _parse_content(result)
        assert "credentials" in content
        assert isinstance(content["credentials"], list)

    def test_self_scope_calls_get_credentials_for_caller(
        self, normal_user, mock_cred_manager
    ):
        """scope='self' calls get_credentials with the caller's username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with patch(
            "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
        ) as mock_deps:
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler({"scope": "self"}, normal_user)

        mock_cred_manager.get_credentials.assert_called_once_with(normal_user.username)


# =============================================================================
# list_mcp_credentials: scope='user' — elevation required
# =============================================================================


class TestListMCPCredentialsUserScope:
    """scope='user' lists specific user's creds — requires elevation and username."""

    def test_user_scope_missing_username_returns_error(self, admin_user, tmp_path):
        """scope='user' without username returns success=False."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with _active_elevation(admin_user.username, tmp_path):
            result = handler(
                {"scope": "user"}, admin_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert content["success"] is False
        assert "username" in content.get("error", "").lower()

    def test_user_scope_with_username_returns_success(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """scope='user' with username returns success=True with elevation."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {"scope": "user", "username": "target_user"},
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_user_scope_calls_get_credentials_for_target_user(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """scope='user' calls get_credentials with the target username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler(
                {"scope": "user", "username": "target_user"},
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        mock_cred_manager.get_credentials.assert_called_once_with("target_user")

    def test_user_scope_without_elevation_returns_elevation_error(
        self, admin_user, tmp_path
    ):
        """scope='user' without active elevation returns elevation error (not success)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        # Enforce elevation but provide NO active window
        with patch(_ENFORCEMENT_PATH, return_value=True):
            result = handler(
                {"scope": "user", "username": "target"},
                admin_user,
                session_key=None,
            )

        # Should be some kind of error (elevation error dict or content with error)
        # Key: must NOT be success=True
        if "content" in result:
            content = _parse_content(result)
            assert content.get("success") is not True
        else:
            assert "error" in result


# =============================================================================
# list_mcp_credentials: scope='all' — elevation required
# =============================================================================


class TestListMCPCredentialsAllScope:
    """scope='all' lists all users' creds — requires elevation."""

    def test_all_scope_returns_success_with_elevation(
        self, admin_user, mock_cred_manager, mock_user_manager, tmp_path
    ):
        """scope='all' returns success=True with elevation."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        mock_cred_manager.get_credentials.side_effect = [
            [
                {
                    "credential_id": "c1",
                    "name": "u1",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ],
            [
                {
                    "credential_id": "c2",
                    "name": "u2",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ],
        ]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials._utils"
            ) as mock_utils,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            mock_utils.app_module.user_manager = mock_user_manager
            result = handler(
                {"scope": "all"}, admin_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_all_scope_includes_username_in_each_credential(
        self, admin_user, mock_cred_manager, mock_user_manager, tmp_path
    ):
        """scope='all' includes username field in each credential entry."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        mock_cred_manager.get_credentials.side_effect = [
            [
                {
                    "credential_id": "c1",
                    "name": "u1",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ],
            [],
        ]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials._utils"
            ) as mock_utils,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            mock_utils.app_module.user_manager = mock_user_manager
            result = handler(
                {"scope": "all"}, admin_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        for cred in content["credentials"]:
            assert "username" in cred


# =============================================================================
# list_mcp_credentials: scope='system' — elevation required
# =============================================================================


class TestListMCPCredentialsSystemScope:
    """scope='system' lists system-managed creds — requires elevation and admin role."""

    def test_system_scope_admin_returns_success(
        self, admin_user, mock_user_manager, tmp_path
    ):
        """scope='system' with admin user returns success=True."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.user_manager = mock_user_manager
            result = handler(
                {"scope": "system"}, admin_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_system_scope_non_admin_returns_permission_error(
        self, normal_user, tmp_path
    ):
        """scope='system' with non-admin user returns permission denied."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with _active_elevation(normal_user.username, tmp_path):
            result = handler(
                {"scope": "system"}, normal_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert content["success"] is False
        assert (
            "permission" in content.get("error", "").lower()
            or "admin" in content.get("error", "").lower()
        )

    def test_system_scope_returns_system_credentials(
        self, admin_user, mock_user_manager, tmp_path
    ):
        """scope='system' returns system_credentials key."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.user_manager = mock_user_manager
            result = handler(
                {"scope": "system"}, admin_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert "system_credentials" in content


# =============================================================================
# manage_mcp_credential: missing action parameter
# =============================================================================


class TestManageMCPCredentialMissingAction:
    """manage_mcp_credential returns clear error when action is missing."""

    def test_missing_action_returns_error(self, normal_user):
        """manage_mcp_credential({}) returns success=False with clear error."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        result = handler({}, normal_user)
        content = _parse_content(result)

        assert content["success"] is False
        assert "action" in content.get("error", "").lower()

    def test_unknown_action_returns_error(self, normal_user):
        """manage_mcp_credential with unknown action returns success=False."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        result = handler({"action": "bogus"}, normal_user)
        content = _parse_content(result)

        assert content["success"] is False


# =============================================================================
# manage_mcp_credential: action='create' (self)
# =============================================================================


class TestManageMCPCredentialCreateSelf:
    """action='create' without target_user creates cred for caller — elevation required."""

    def test_create_self_returns_success_with_elevation(
        self, normal_user, mock_cred_manager, tmp_path
    ):
        """action='create' (no target_user) returns success=True with elevation."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {"action": "create"}, normal_user, session_key=_TEST_SESSION_KEY
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_create_self_returns_credential_id(
        self, normal_user, mock_cred_manager, tmp_path
    ):
        """action='create' (no target_user) returns credential_id in response."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {"action": "create", "description": "My cred"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert "credential_id" in content

    def test_create_self_calls_generate_credential_for_caller(
        self, normal_user, mock_cred_manager, tmp_path
    ):
        """action='create' (no target_user) calls generate_credential with caller's username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler(
                {"action": "create"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

        mock_cred_manager.generate_credential.assert_called_once_with(
            normal_user.username, name=""
        )

    def test_create_self_without_elevation_is_blocked(self, normal_user, tmp_path):
        """action='create' without elevation is blocked (not success)."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with patch(_ENFORCEMENT_PATH, return_value=True):
            result = handler({"action": "create"}, normal_user, session_key=None)

        if "content" in result:
            content = _parse_content(result)
            assert content.get("success") is not True
        else:
            assert "error" in result


# =============================================================================
# manage_mcp_credential: action='delete' (self)
# =============================================================================


class TestManageMCPCredentialDeleteSelf:
    """action='delete' without target_user deletes caller's cred — elevation required."""

    def test_delete_self_returns_success_with_elevation(
        self, normal_user, mock_cred_manager, tmp_path
    ):
        """action='delete' (no target_user) with credential_id returns success."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {"action": "delete", "credential_id": "cred-123"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_delete_self_missing_credential_id_returns_error(
        self, normal_user, tmp_path
    ):
        """action='delete' (no target_user) without credential_id returns error."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with _active_elevation(normal_user.username, tmp_path):
            result = handler(
                {"action": "delete"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is False
        assert "credential_id" in content.get("error", "").lower()

    def test_delete_self_calls_revoke_credential_for_caller(
        self, normal_user, mock_cred_manager, tmp_path
    ):
        """action='delete' (no target_user) calls revoke_credential with caller's username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(normal_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler(
                {"action": "delete", "credential_id": "cred-123"},
                normal_user,
                session_key=_TEST_SESSION_KEY,
            )

        mock_cred_manager.revoke_credential.assert_called_once_with(
            normal_user.username, "cred-123"
        )


# =============================================================================
# manage_mcp_credential: action='create' (admin, with target_user)
# =============================================================================


class TestManageMCPCredentialCreateAdmin:
    """action='create' with target_user creates cred for target — elevation required."""

    def test_admin_create_returns_success_with_elevation(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """action='create' with target_user returns success=True with elevation."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {"action": "create", "target_user": "alice"},
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_admin_create_calls_generate_credential_for_target(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """action='create' with target_user calls generate_credential with target username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler(
                {
                    "action": "create",
                    "target_user": "alice",
                    "description": "Admin cred",
                },
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        mock_cred_manager.generate_credential.assert_called_once_with(
            "alice", name="Admin cred"
        )


# =============================================================================
# manage_mcp_credential: action='delete' (admin, with target_user)
# =============================================================================


class TestManageMCPCredentialDeleteAdmin:
    """action='delete' with target_user deletes target's cred — elevation required."""

    def test_admin_delete_returns_success_with_elevation(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """action='delete' with target_user and credential_id returns success."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler(
                {
                    "action": "delete",
                    "target_user": "alice",
                    "credential_id": "cred-456",
                },
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is True

    def test_admin_delete_missing_credential_id_returns_error(
        self, admin_user, tmp_path
    ):
        """action='delete' with target_user but missing credential_id returns error."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with _active_elevation(admin_user.username, tmp_path):
            result = handler(
                {"action": "delete", "target_user": "alice"},
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        content = _parse_content(result)
        assert content["success"] is False
        assert "credential_id" in content.get("error", "").lower()

    def test_admin_delete_calls_revoke_credential_for_target(
        self, admin_user, mock_cred_manager, tmp_path
    ):
        """action='delete' with target_user calls revoke_credential with target username."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        with (
            _active_elevation(admin_user.username, tmp_path),
            patch(
                "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
            ) as mock_deps,
        ):
            mock_deps.mcp_credential_manager = mock_cred_manager
            handler(
                {
                    "action": "delete",
                    "target_user": "alice",
                    "credential_id": "cred-456",
                },
                admin_user,
                session_key=_TEST_SESSION_KEY,
            )

        mock_cred_manager.revoke_credential.assert_called_once_with("alice", "cred-456")


# =============================================================================
# Elevation structural check: inner handlers must be decorated
# =============================================================================


class TestElevationStructure:
    """Inner handlers that require elevation must have __wrapped__ attribute."""

    def test_list_user_inner_handler_is_elevation_wrapped(self):
        """_list_user inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._list_user, "__wrapped__"), (
            "_list_user must be decorated with @require_mcp_elevation()"
        )

    def test_list_all_inner_handler_is_elevation_wrapped(self):
        """_list_all inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._list_all, "__wrapped__"), (
            "_list_all must be decorated with @require_mcp_elevation()"
        )

    def test_list_system_inner_handler_is_elevation_wrapped(self):
        """_list_system inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._list_system, "__wrapped__"), (
            "_list_system must be decorated with @require_mcp_elevation()"
        )

    def test_create_self_inner_handler_is_elevation_wrapped(self):
        """_create_self inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._create_self, "__wrapped__"), (
            "_create_self must be decorated with @require_mcp_elevation()"
        )

    def test_delete_self_inner_handler_is_elevation_wrapped(self):
        """_delete_self inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._delete_self, "__wrapped__"), (
            "_delete_self must be decorated with @require_mcp_elevation()"
        )

    def test_create_user_inner_handler_is_elevation_wrapped(self):
        """_create_user inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._create_user, "__wrapped__"), (
            "_create_user must be decorated with @require_mcp_elevation()"
        )

    def test_delete_user_inner_handler_is_elevation_wrapped(self):
        """_delete_user inner handler has __wrapped__ (elevation applied)."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert hasattr(mod._delete_user, "__wrapped__"), (
            "_delete_user must be decorated with @require_mcp_elevation()"
        )

    def test_list_self_inner_handler_is_NOT_elevation_wrapped(self):
        """_list_self inner handler must NOT have __wrapped__ — no elevation for self-list."""
        import code_indexer.server.mcp.handlers.admin.mcp_credentials as mod

        assert not hasattr(mod._list_self, "__wrapped__"), (
            "_list_self must NOT be decorated with @require_mcp_elevation() "
            "— the old handle_list_mcp_credentials was undecorated"
        )


# =============================================================================
# MCP response format compliance
# =============================================================================


class TestMCPResponseFormat:
    """All handlers must return MCP-compliant content arrays."""

    def test_list_mcp_credentials_self_returns_mcp_format(
        self, normal_user, mock_cred_manager
    ):
        """list_mcp_credentials(scope='self') returns content[0].type='text'."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        with patch(
            "code_indexer.server.mcp.handlers.admin.mcp_credentials.dependencies"
        ) as mock_deps:
            mock_deps.mcp_credential_manager = mock_cred_manager
            result = handler({"scope": "self"}, normal_user)

        assert "content" in result
        assert isinstance(result["content"], list)
        assert result["content"][0]["type"] == "text"
        json.loads(result["content"][0]["text"])  # must be valid JSON

    def test_list_mcp_credentials_missing_scope_returns_mcp_format(self, normal_user):
        """list_mcp_credentials({}) error response is MCP-compliant."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["list_mcp_credentials"]
        result = handler({}, normal_user)

        assert "content" in result
        json.loads(result["content"][0]["text"])

    def test_manage_mcp_credential_missing_action_returns_mcp_format(self, normal_user):
        """manage_mcp_credential({}) error response is MCP-compliant."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY["manage_mcp_credential"]
        result = handler({}, normal_user)

        assert "content" in result
        json.loads(result["content"][0]["text"])
