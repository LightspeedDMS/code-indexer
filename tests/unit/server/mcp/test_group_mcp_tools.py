"""
Unit tests for Group & Access Management MCP Tools.

Story #742: Implement 9 new MCP tools for group and access management.

TDD Approach: These tests are written FIRST before implementation.
All tests should FAIL initially until the tools are implemented.

Tests verify:
1. Tool schemas exist in TOOL_REGISTRY
2. Tool handlers exist in HANDLER_REGISTRY
3. Schema validation for each tool
4. Permission requirements are set correctly
5. Input/output schema correctness
"""

import contextlib
import pytest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Elevation helpers (Story #925 AC2 — gated group handlers)
# ---------------------------------------------------------------------------

_ELEVATION_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_ELEVATION_ESM_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"
)
_ELEVATION_TOTP_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
)
_TEST_SESSION_KEY = "test-elevation-session-key"
_TEST_ELEVATION_IDLE_TIMEOUT_SECONDS = 300
_TEST_ELEVATION_MAX_AGE_SECONDS = 1800


@contextlib.contextmanager
def _with_elevation(username: str, tmp_path_str: str):
    """Patch elevation decorator deps to provide an active full-scope window.

    Patches enforcement=True, a real ElevatedSessionManager with an active
    session, and a mock TOTP service with MFA enabled (all three are required
    by require_mcp_elevation).  Yields the session_key so tests can pass it
    to gated handlers via session_key kwarg.
    """
    esm = ElevatedSessionManager(
        idle_timeout_seconds=_TEST_ELEVATION_IDLE_TIMEOUT_SECONDS,
        max_age_seconds=_TEST_ELEVATION_MAX_AGE_SECONDS,
        db_path=str(Path(tmp_path_str) / "elev.db"),
    )
    esm.create(
        session_key=_TEST_SESSION_KEY,
        username=username,
        elevated_from_ip=None,
        scope="full",
    )
    totp_svc = MagicMock()
    totp_svc.is_mfa_enabled.return_value = True
    with (
        patch(_ELEVATION_ENFORCEMENT_PATH, return_value=True),
        patch(_ELEVATION_ESM_PATH, esm),
        patch(_ELEVATION_TOTP_PATH, return_value=totp_svc),
    ):
        yield _TEST_SESSION_KEY


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def admin_user():
    """Create an admin user for testing."""
    return User(
        username="admin_test",
        password_hash="$2b$12$hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def normal_user():
    """Create a normal user for testing."""
    return User(
        username="normal_test",
        password_hash="$2b$12$hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def temp_groups_db():
    """Create a temporary database for groups testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


# =============================================================================
# Tool Schema Existence Tests (AC1-AC9: All tools exist in registry)
# =============================================================================


class TestGroupMCPToolsExistInRegistry:
    """Verify all 9 group MCP tools exist in TOOL_REGISTRY."""

    def test_list_groups_exists_in_registry(self):
        """AC1: list_groups tool exists in TOOL_REGISTRY."""
        assert "list_groups" in TOOL_REGISTRY
        assert TOOL_REGISTRY["list_groups"]["name"] == "list_groups"

    def test_create_group_exists_in_registry(self):
        """AC2: create_group tool exists in TOOL_REGISTRY."""
        assert "create_group" in TOOL_REGISTRY
        assert TOOL_REGISTRY["create_group"]["name"] == "create_group"

    def test_get_group_exists_in_registry(self):
        """AC3: get_group tool exists in TOOL_REGISTRY."""
        assert "get_group" in TOOL_REGISTRY
        assert TOOL_REGISTRY["get_group"]["name"] == "get_group"

    def test_update_group_exists_in_registry(self):
        """AC4: update_group tool exists in TOOL_REGISTRY."""
        assert "update_group" in TOOL_REGISTRY
        assert TOOL_REGISTRY["update_group"]["name"] == "update_group"

    def test_delete_group_exists_in_registry(self):
        """AC5: delete_group tool exists in TOOL_REGISTRY."""
        assert "delete_group" in TOOL_REGISTRY
        assert TOOL_REGISTRY["delete_group"]["name"] == "delete_group"

    def test_manage_group_members_exists_in_registry(self):
        """AC6+AC6b: manage_group_members unified tool exists in TOOL_REGISTRY (Story #992)."""
        assert "manage_group_members" in TOOL_REGISTRY
        assert TOOL_REGISTRY["manage_group_members"]["name"] == "manage_group_members"

    def test_manage_group_repos_exists_in_registry(self):
        """AC7+AC8+AC9: manage_group_repos unified tool exists in TOOL_REGISTRY (Story #992)."""
        assert "manage_group_repos" in TOOL_REGISTRY
        assert TOOL_REGISTRY["manage_group_repos"]["name"] == "manage_group_repos"


# =============================================================================
# Tool Handler Existence Tests
# =============================================================================


class TestGroupMCPHandlersExistInRegistry:
    """Verify all 9 group MCP tool handlers exist in HANDLER_REGISTRY."""

    def test_list_groups_handler_exists(self):
        """list_groups handler exists in HANDLER_REGISTRY."""
        assert "list_groups" in HANDLER_REGISTRY

    def test_create_group_handler_exists(self):
        """create_group handler exists in HANDLER_REGISTRY."""
        assert "create_group" in HANDLER_REGISTRY

    def test_get_group_handler_exists(self):
        """get_group handler exists in HANDLER_REGISTRY."""
        assert "get_group" in HANDLER_REGISTRY

    def test_update_group_handler_exists(self):
        """update_group handler exists in HANDLER_REGISTRY."""
        assert "update_group" in HANDLER_REGISTRY

    def test_delete_group_handler_exists(self):
        """delete_group handler exists in HANDLER_REGISTRY."""
        assert "delete_group" in HANDLER_REGISTRY

    def test_manage_group_members_handler_exists(self):
        """manage_group_members handler exists in HANDLER_REGISTRY (Story #992)."""
        assert "manage_group_members" in HANDLER_REGISTRY

    def test_manage_group_repos_handler_exists(self):
        """manage_group_repos handler exists in HANDLER_REGISTRY (Story #992)."""
        assert "manage_group_repos" in HANDLER_REGISTRY


# =============================================================================
# Permission Requirements Tests
# =============================================================================


class TestGroupMCPToolsPermissions:
    """Verify all 9 group MCP tools have correct permission requirements."""

    def test_list_groups_requires_manage_users_permission(self):
        """list_groups tool requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["list_groups"]["required_permission"] == "manage_users"

    def test_create_group_requires_manage_users_permission(self):
        """create_group tool requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["create_group"]["required_permission"] == "manage_users"

    def test_get_group_requires_manage_users_permission(self):
        """get_group tool requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["get_group"]["required_permission"] == "manage_users"

    def test_update_group_requires_manage_users_permission(self):
        """update_group tool requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["update_group"]["required_permission"] == "manage_users"

    def test_delete_group_requires_manage_users_permission(self):
        """delete_group tool requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["delete_group"]["required_permission"] == "manage_users"

    def test_manage_group_members_requires_manage_users_permission(self):
        """manage_group_members unified tool requires manage_users permission (Story #992)."""
        assert (
            TOOL_REGISTRY["manage_group_members"]["required_permission"]
            == "manage_users"
        )

    def test_manage_group_repos_requires_manage_users_permission(self):
        """manage_group_repos unified tool requires manage_users permission (Story #992)."""
        assert (
            TOOL_REGISTRY["manage_group_repos"]["required_permission"] == "manage_users"
        )


# =============================================================================
# Input Schema Validation Tests
# =============================================================================


class TestListGroupsSchema:
    """Tests for list_groups tool schema."""

    def test_list_groups_has_input_schema(self):
        """list_groups tool has inputSchema defined."""
        schema = TOOL_REGISTRY["list_groups"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_list_groups_has_no_required_properties(self):
        """list_groups requires no inputs."""
        schema = TOOL_REGISTRY["list_groups"]["inputSchema"]
        assert schema.get("required", []) == []

    def test_list_groups_has_description(self):
        """list_groups has a meaningful description."""
        tool = TOOL_REGISTRY["list_groups"]
        assert "description" in tool
        assert len(tool["description"]) > 10


class TestCreateGroupSchema:
    """Tests for create_group tool schema."""

    def test_create_group_has_input_schema(self):
        """create_group tool has inputSchema defined."""
        schema = TOOL_REGISTRY["create_group"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_create_group_requires_name(self):
        """create_group requires name parameter."""
        schema = TOOL_REGISTRY["create_group"]["inputSchema"]
        assert "name" in schema["properties"]
        assert "name" in schema.get("required", [])

    def test_create_group_name_is_string(self):
        """create_group name parameter is a string."""
        schema = TOOL_REGISTRY["create_group"]["inputSchema"]
        assert schema["properties"]["name"]["type"] == "string"

    def test_create_group_description_is_optional(self):
        """create_group description parameter is optional."""
        schema = TOOL_REGISTRY["create_group"]["inputSchema"]
        assert "description" in schema["properties"]
        assert "description" not in schema.get("required", [])


class TestGetGroupSchema:
    """Tests for get_group tool schema."""

    def test_get_group_has_input_schema(self):
        """get_group tool has inputSchema defined."""
        schema = TOOL_REGISTRY["get_group"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_get_group_requires_group_id(self):
        """get_group requires group_id parameter."""
        schema = TOOL_REGISTRY["get_group"]["inputSchema"]
        assert "group_id" in schema["properties"]
        assert "group_id" in schema.get("required", [])


class TestUpdateGroupSchema:
    """Tests for update_group tool schema."""

    def test_update_group_has_input_schema(self):
        """update_group tool has inputSchema defined."""
        schema = TOOL_REGISTRY["update_group"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_update_group_requires_group_id(self):
        """update_group requires group_id parameter."""
        schema = TOOL_REGISTRY["update_group"]["inputSchema"]
        assert "group_id" in schema["properties"]
        assert "group_id" in schema.get("required", [])

    def test_update_group_name_is_optional(self):
        """update_group name parameter is optional."""
        schema = TOOL_REGISTRY["update_group"]["inputSchema"]
        assert "name" in schema["properties"]
        assert "name" not in schema.get("required", [])

    def test_update_group_description_is_optional(self):
        """update_group description parameter is optional."""
        schema = TOOL_REGISTRY["update_group"]["inputSchema"]
        assert "description" in schema["properties"]
        assert "description" not in schema.get("required", [])


class TestDeleteGroupSchema:
    """Tests for delete_group tool schema."""

    def test_delete_group_has_input_schema(self):
        """delete_group tool has inputSchema defined."""
        schema = TOOL_REGISTRY["delete_group"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_delete_group_requires_group_id(self):
        """delete_group requires group_id parameter."""
        schema = TOOL_REGISTRY["delete_group"]["inputSchema"]
        assert "group_id" in schema["properties"]
        assert "group_id" in schema.get("required", [])


class TestManageGroupMembersSchema:
    """Tests for manage_group_members unified tool schema (Story #992)."""

    def test_manage_group_members_has_input_schema(self):
        """manage_group_members tool has inputSchema defined."""
        schema = TOOL_REGISTRY["manage_group_members"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_manage_group_members_requires_action(self):
        """manage_group_members requires action parameter."""
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "action" in schema["properties"]
        assert "action" in schema.get("required", [])

    def test_manage_group_members_requires_group_id(self):
        """manage_group_members requires group_id parameter."""
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "group_id" in schema["properties"]

    def test_manage_group_members_requires_user_id(self):
        """manage_group_members requires user_id parameter."""
        schema = TOOL_REGISTRY["manage_group_members"]["inputSchema"]
        assert "user_id" in schema["properties"]

    def test_manage_group_members_has_description(self):
        """manage_group_members has a meaningful description."""
        tool = TOOL_REGISTRY["manage_group_members"]
        assert "description" in tool
        assert len(tool["description"]) > 10


class TestManageGroupReposSchema:
    """Tests for manage_group_repos unified tool schema (Story #992)."""

    def test_manage_group_repos_has_input_schema(self):
        """manage_group_repos tool has inputSchema defined."""
        schema = TOOL_REGISTRY["manage_group_repos"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_manage_group_repos_requires_action(self):
        """manage_group_repos requires action parameter."""
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "action" in schema["properties"]
        assert "action" in schema.get("required", [])

    def test_manage_group_repos_requires_group_id(self):
        """manage_group_repos requires group_id parameter."""
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "group_id" in schema["properties"]

    def test_manage_group_repos_has_repos_array(self):
        """manage_group_repos has repos array parameter."""
        schema = TOOL_REGISTRY["manage_group_repos"]["inputSchema"]
        assert "repos" in schema["properties"]
        assert schema["properties"]["repos"]["type"] == "array"
        assert schema["properties"]["repos"]["items"]["type"] == "string"


# =============================================================================
# Handler Functional Tests (Integration with GroupAccessManager)
# =============================================================================


class TestListGroupsHandler:
    """Tests for list_groups handler functionality."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        """Create mock group manager for testing handlers."""
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        manager = GroupAccessManager(temp_groups_db)
        return manager

    def test_list_groups_returns_success_true(self, admin_user, mock_group_manager):
        """list_groups handler returns success=True on valid call."""
        with patch(
            "code_indexer.server.mcp.handlers._get_group_manager",
            return_value=mock_group_manager,
        ):
            handler = HANDLER_REGISTRY["list_groups"]
            result = handler({}, admin_user)

            # Parse MCP response format
            assert "content" in result
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_list_groups_returns_groups_array(self, admin_user, mock_group_manager):
        """list_groups handler returns groups array."""
        with patch(
            "code_indexer.server.mcp.handlers._get_group_manager",
            return_value=mock_group_manager,
        ):
            handler = HANDLER_REGISTRY["list_groups"]
            result = handler({}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert "groups" in content
            assert isinstance(content["groups"], list)

    def test_list_groups_returns_default_groups(self, admin_user, mock_group_manager):
        """list_groups handler returns default groups (admins, powerusers, users)."""
        with patch(
            "code_indexer.server.mcp.handlers._get_group_manager",
            return_value=mock_group_manager,
        ):
            handler = HANDLER_REGISTRY["list_groups"]
            result = handler({}, admin_user)

            content = json.loads(result["content"][0]["text"])
            group_names = {g["name"] for g in content["groups"]}
            assert "admins" in group_names
            assert "powerusers" in group_names
            assert "users" in group_names


class TestCreateGroupHandler:
    """Tests for create_group handler functionality."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        """Create mock group manager for testing handlers."""
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        manager = GroupAccessManager(temp_groups_db)
        return manager

    def test_create_group_returns_success(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """create_group handler returns success on valid creation."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            handler = HANDLER_REGISTRY["create_group"]
            result = handler(
                {"name": "test_group", "description": "Test description"},
                admin_user,
                session_key=session_key,
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True
            assert "group_id" in content
            assert content["name"] == "test_group"

    def test_create_group_duplicate_fails(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """create_group handler fails for duplicate group name."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            handler = HANDLER_REGISTRY["create_group"]
            # First creation should succeed
            handler({"name": "unique_group"}, admin_user, session_key=session_key)
            # Second creation should fail
            result = handler(
                {"name": "unique_group"}, admin_user, session_key=session_key
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is False
            assert "error" in content


class TestGetGroupHandler:
    """Tests for get_group handler functionality."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        """Create mock group manager for testing handlers."""
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        manager = GroupAccessManager(temp_groups_db)
        return manager

    def test_get_group_returns_details(self, admin_user, mock_group_manager):
        """get_group handler returns group details."""
        with patch(
            "code_indexer.server.mcp.handlers._get_group_manager",
            return_value=mock_group_manager,
        ):
            # First get a valid group ID
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id

            handler = HANDLER_REGISTRY["get_group"]
            result = handler({"group_id": str(group_id)}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True
            assert "id" in content
            assert "name" in content
            assert "description" in content
            assert "members" in content
            assert "repos" in content

    def test_get_group_invalid_id_fails(self, admin_user, mock_group_manager):
        """get_group handler fails for invalid group ID."""
        with patch(
            "code_indexer.server.mcp.handlers._get_group_manager",
            return_value=mock_group_manager,
        ):
            handler = HANDLER_REGISTRY["get_group"]
            result = handler({"group_id": "99999"}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is False
            assert "error" in content


class TestUpdateGroupHandler:
    """Tests for update_group handler functionality."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        """Create mock group manager for testing handlers."""
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        manager = GroupAccessManager(temp_groups_db)
        return manager

    def test_update_group_returns_success(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """update_group handler returns success on valid update."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            # Create a custom group first
            custom_group = mock_group_manager.create_group(
                "custom_test", "Test description"
            )

            handler = HANDLER_REGISTRY["update_group"]
            result = handler(
                {
                    "group_id": str(custom_group.id),
                    "name": "updated_name",
                    "description": "Updated description",
                },
                admin_user,
                session_key=session_key,
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True


class TestDeleteGroupHandler:
    """Tests for delete_group handler functionality."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        """Create mock group manager for testing handlers."""
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        manager = GroupAccessManager(temp_groups_db)
        return manager

    def test_delete_group_custom_succeeds(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """delete_group handler succeeds for custom groups."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            # Create a custom group first
            custom_group = mock_group_manager.create_group(
                "to_delete", "Will be deleted"
            )

            handler = HANDLER_REGISTRY["delete_group"]
            result = handler(
                {"group_id": str(custom_group.id)}, admin_user, session_key=session_key
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_delete_group_default_fails(self, admin_user, mock_group_manager, tmp_path):
        """delete_group handler fails for default groups."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            # Get the admins default group
            admins = mock_group_manager.get_group_by_name("admins")

            handler = HANDLER_REGISTRY["delete_group"]
            result = handler(
                {"group_id": str(admins.id)}, admin_user, session_key=session_key
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is False
            assert "error" in content


class TestManageGroupMembersHandler:
    """Tests for manage_group_members unified handler (Story #992)."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        return GroupAccessManager(temp_groups_db)

    def test_add_member_succeeds(self, admin_user, mock_group_manager, tmp_path):
        """manage_group_members add action succeeds for valid inputs."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id

            handler = HANDLER_REGISTRY["manage_group_members"]
            result = handler(
                {"action": "add", "group_id": str(group_id), "user_id": "test_user"},
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_remove_member_succeeds(self, admin_user, mock_group_manager, tmp_path):
        """manage_group_members remove action succeeds for valid inputs."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id
            mock_group_manager.assign_user_to_group("test_user", group_id, "admin_test")

            handler = HANDLER_REGISTRY["manage_group_members"]
            result = handler(
                {"action": "remove", "group_id": str(group_id), "user_id": "test_user"},
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_remove_nonexistent_user_returns_success_field(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """manage_group_members remove for nonexistent user returns success field."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id

            handler = HANDLER_REGISTRY["manage_group_members"]
            result = handler(
                {
                    "action": "remove",
                    "group_id": str(group_id),
                    "user_id": "nonexistent_user",
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert "success" in content


@pytest.mark.slow
class TestManageGroupReposHandler:
    """Tests for manage_group_repos unified handler (Story #992)."""

    @pytest.fixture
    def mock_group_manager(self, temp_groups_db):
        from code_indexer.server.services.group_access_manager import GroupAccessManager

        return GroupAccessManager(temp_groups_db)

    def test_add_repos_succeeds(self, admin_user, mock_group_manager, tmp_path):
        """manage_group_repos add action succeeds for valid inputs."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id

            handler = HANDLER_REGISTRY["manage_group_repos"]
            result = handler(
                {
                    "action": "add",
                    "group_id": str(group_id),
                    "repos": ["repo1", "repo2"],
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True
            assert "added_count" in content

    def test_add_repos_returns_correct_count(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """manage_group_repos add returns count of repos added."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id

            handler = HANDLER_REGISTRY["manage_group_repos"]
            result = handler(
                {
                    "action": "add",
                    "group_id": str(group_id),
                    "repos": ["repo1", "repo2", "repo3"],
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["added_count"] == 3

    def test_remove_repo_succeeds(self, admin_user, mock_group_manager, tmp_path):
        """manage_group_repos remove action succeeds for existing repo."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id
            mock_group_manager.grant_repo_access("test_repo", group_id, "admin_test")

            handler = HANDLER_REGISTRY["manage_group_repos"]
            result = handler(
                {
                    "action": "remove",
                    "group_id": str(group_id),
                    "repo_name": "test_repo",
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_bulk_remove_repos_succeeds(self, admin_user, mock_group_manager, tmp_path):
        """manage_group_repos bulk_remove action succeeds for valid inputs."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id
            mock_group_manager.grant_repo_access("repo1", group_id, "admin_test")
            mock_group_manager.grant_repo_access("repo2", group_id, "admin_test")

            handler = HANDLER_REGISTRY["manage_group_repos"]
            result = handler(
                {
                    "action": "bulk_remove",
                    "group_id": str(group_id),
                    "repos": ["repo1", "repo2"],
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True
            assert "removed_count" in content

    def test_bulk_remove_returns_correct_count(
        self, admin_user, mock_group_manager, tmp_path
    ):
        """manage_group_repos bulk_remove returns count of repos removed."""
        with (
            patch(
                "code_indexer.server.mcp.handlers.admin._get_group_manager",
                return_value=mock_group_manager,
            ),
            _with_elevation(admin_user.username, str(tmp_path)) as session_key,
        ):
            groups = mock_group_manager.get_all_groups()
            group_id = groups[0].id
            mock_group_manager.grant_repo_access("repo_a", group_id, "admin_test")
            mock_group_manager.grant_repo_access("repo_b", group_id, "admin_test")

            handler = HANDLER_REGISTRY["manage_group_repos"]
            result = handler(
                {
                    "action": "bulk_remove",
                    "group_id": str(group_id),
                    "repos": ["repo_a", "repo_b"],
                },
                admin_user,
                session_key=session_key,
            )
            content = json.loads(result["content"][0]["text"])
            assert content["removed_count"] == 2
