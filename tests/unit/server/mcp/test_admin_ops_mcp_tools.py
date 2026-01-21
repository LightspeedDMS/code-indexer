"""
Unit tests for Admin Operations MCP Tools - Story #744.

TDD Approach: These tests are written FIRST before implementation.

Implements 8 MCP tools for administrative operations:
1. query_audit_logs - Query audit logs with filtering
2. enter_maintenance_mode - Enter maintenance mode
3. exit_maintenance_mode - Exit maintenance mode
4. get_maintenance_status - Get maintenance mode status
5. scip_pr_history - Get SCIP PR creation history
6. scip_cleanup_history - Get SCIP workspace cleanup history
7. scip_cleanup_workspaces - Trigger workspace cleanup
8. scip_cleanup_status - Get cleanup job status

Tests verify:
1. Tool schemas exist in TOOL_REGISTRY
2. Tool handlers exist in HANDLER_REGISTRY
3. Permission requirements are set correctly
4. Input schemas are properly defined
"""

import pytest
from datetime import datetime, timezone

from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole


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


# =============================================================================
# Tool Schema Existence Tests - Audit Logs (1 tool)
# =============================================================================


class TestAuditLogToolExistsInRegistry:
    """Verify audit log MCP tool exists in TOOL_REGISTRY."""

    def test_query_audit_logs_exists_in_registry(self):
        """AC1: query_audit_logs tool exists in TOOL_REGISTRY."""
        assert "query_audit_logs" in TOOL_REGISTRY
        assert TOOL_REGISTRY["query_audit_logs"]["name"] == "query_audit_logs"

    def test_query_audit_logs_has_correct_permission(self):
        """query_audit_logs requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["query_audit_logs"]["required_permission"] == "manage_users"
        )

    def test_query_audit_logs_has_input_schema(self):
        """query_audit_logs has proper inputSchema."""
        schema = TOOL_REGISTRY["query_audit_logs"]["inputSchema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        # All inputs are optional
        assert "user" in props
        assert "action" in props
        assert "from_date" in props
        assert "to_date" in props
        assert "limit" in props


# =============================================================================
# Tool Schema Existence Tests - Maintenance Mode (3 tools)
# =============================================================================


class TestMaintenanceModeToolsExistInRegistry:
    """Verify all 3 maintenance mode MCP tools exist in TOOL_REGISTRY."""

    def test_enter_maintenance_mode_exists_in_registry(self):
        """AC2: enter_maintenance_mode tool exists in TOOL_REGISTRY."""
        assert "enter_maintenance_mode" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["enter_maintenance_mode"]["name"] == "enter_maintenance_mode"
        )

    def test_enter_maintenance_mode_has_correct_permission(self):
        """enter_maintenance_mode requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["enter_maintenance_mode"]["required_permission"]
            == "manage_users"
        )

    def test_enter_maintenance_mode_has_input_schema(self):
        """enter_maintenance_mode has proper inputSchema."""
        schema = TOOL_REGISTRY["enter_maintenance_mode"]["inputSchema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        # Optional message parameter
        assert "message" in props

    def test_exit_maintenance_mode_exists_in_registry(self):
        """AC3: exit_maintenance_mode tool exists in TOOL_REGISTRY."""
        assert "exit_maintenance_mode" in TOOL_REGISTRY
        assert TOOL_REGISTRY["exit_maintenance_mode"]["name"] == "exit_maintenance_mode"

    def test_exit_maintenance_mode_has_correct_permission(self):
        """exit_maintenance_mode requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["exit_maintenance_mode"]["required_permission"]
            == "manage_users"
        )

    def test_exit_maintenance_mode_has_empty_input_schema(self):
        """exit_maintenance_mode has empty inputSchema (no inputs)."""
        schema = TOOL_REGISTRY["exit_maintenance_mode"]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["required"] == []

    def test_get_maintenance_status_exists_in_registry(self):
        """AC4: get_maintenance_status tool exists in TOOL_REGISTRY."""
        assert "get_maintenance_status" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["get_maintenance_status"]["name"] == "get_maintenance_status"
        )

    def test_get_maintenance_status_has_query_repos_permission(self):
        """get_maintenance_status requires query_repos permission (any user)."""
        # This is intentionally query_repos, not manage_users - any authenticated
        # user should be able to check if the system is in maintenance mode
        assert (
            TOOL_REGISTRY["get_maintenance_status"]["required_permission"]
            == "query_repos"
        )

    def test_get_maintenance_status_has_empty_input_schema(self):
        """get_maintenance_status has empty inputSchema (no inputs)."""
        schema = TOOL_REGISTRY["get_maintenance_status"]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["required"] == []


# =============================================================================
# Tool Schema Existence Tests - SCIP Administration (4 tools)
# =============================================================================


class TestSCIPAdminToolsExistInRegistry:
    """Verify all 4 SCIP administration MCP tools exist in TOOL_REGISTRY."""

    def test_scip_pr_history_exists_in_registry(self):
        """AC5: scip_pr_history tool exists in TOOL_REGISTRY."""
        assert "scip_pr_history" in TOOL_REGISTRY
        assert TOOL_REGISTRY["scip_pr_history"]["name"] == "scip_pr_history"

    def test_scip_pr_history_has_correct_permission(self):
        """scip_pr_history requires manage_users permission (admin only)."""
        assert TOOL_REGISTRY["scip_pr_history"]["required_permission"] == "manage_users"

    def test_scip_pr_history_has_input_schema(self):
        """scip_pr_history has proper inputSchema."""
        schema = TOOL_REGISTRY["scip_pr_history"]["inputSchema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        # Optional limit parameter
        assert "limit" in props

    def test_scip_cleanup_history_exists_in_registry(self):
        """AC6: scip_cleanup_history tool exists in TOOL_REGISTRY."""
        assert "scip_cleanup_history" in TOOL_REGISTRY
        assert TOOL_REGISTRY["scip_cleanup_history"]["name"] == "scip_cleanup_history"

    def test_scip_cleanup_history_has_correct_permission(self):
        """scip_cleanup_history requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["scip_cleanup_history"]["required_permission"]
            == "manage_users"
        )

    def test_scip_cleanup_history_has_input_schema(self):
        """scip_cleanup_history has proper inputSchema."""
        schema = TOOL_REGISTRY["scip_cleanup_history"]["inputSchema"]
        assert schema["type"] == "object"
        props = schema["properties"]
        # Optional limit parameter
        assert "limit" in props

    def test_scip_cleanup_workspaces_exists_in_registry(self):
        """AC7: scip_cleanup_workspaces tool exists in TOOL_REGISTRY."""
        assert "scip_cleanup_workspaces" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["scip_cleanup_workspaces"]["name"]
            == "scip_cleanup_workspaces"
        )

    def test_scip_cleanup_workspaces_has_correct_permission(self):
        """scip_cleanup_workspaces requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["scip_cleanup_workspaces"]["required_permission"]
            == "manage_users"
        )

    def test_scip_cleanup_workspaces_has_empty_input_schema(self):
        """scip_cleanup_workspaces has empty inputSchema (no inputs)."""
        schema = TOOL_REGISTRY["scip_cleanup_workspaces"]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["required"] == []

    def test_scip_cleanup_status_exists_in_registry(self):
        """AC8: scip_cleanup_status tool exists in TOOL_REGISTRY."""
        assert "scip_cleanup_status" in TOOL_REGISTRY
        assert TOOL_REGISTRY["scip_cleanup_status"]["name"] == "scip_cleanup_status"

    def test_scip_cleanup_status_has_correct_permission(self):
        """scip_cleanup_status requires manage_users permission (admin only)."""
        assert (
            TOOL_REGISTRY["scip_cleanup_status"]["required_permission"]
            == "manage_users"
        )

    def test_scip_cleanup_status_has_empty_input_schema(self):
        """scip_cleanup_status has empty inputSchema (no inputs)."""
        schema = TOOL_REGISTRY["scip_cleanup_status"]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert schema["required"] == []


# =============================================================================
# Tool Handler Existence Tests
# =============================================================================


class TestAdminOpsMCPHandlersExistInRegistry:
    """Verify all 8 admin operations MCP tool handlers exist in HANDLER_REGISTRY."""

    # Audit Logs (1)
    def test_query_audit_logs_handler_exists(self):
        """query_audit_logs handler exists in HANDLER_REGISTRY."""
        assert "query_audit_logs" in HANDLER_REGISTRY

    # Maintenance Mode (3)
    def test_enter_maintenance_mode_handler_exists(self):
        """enter_maintenance_mode handler exists in HANDLER_REGISTRY."""
        assert "enter_maintenance_mode" in HANDLER_REGISTRY

    def test_exit_maintenance_mode_handler_exists(self):
        """exit_maintenance_mode handler exists in HANDLER_REGISTRY."""
        assert "exit_maintenance_mode" in HANDLER_REGISTRY

    def test_get_maintenance_status_handler_exists(self):
        """get_maintenance_status handler exists in HANDLER_REGISTRY."""
        assert "get_maintenance_status" in HANDLER_REGISTRY

    # SCIP Administration (4)
    def test_scip_pr_history_handler_exists(self):
        """scip_pr_history handler exists in HANDLER_REGISTRY."""
        assert "scip_pr_history" in HANDLER_REGISTRY

    def test_scip_cleanup_history_handler_exists(self):
        """scip_cleanup_history handler exists in HANDLER_REGISTRY."""
        assert "scip_cleanup_history" in HANDLER_REGISTRY

    def test_scip_cleanup_workspaces_handler_exists(self):
        """scip_cleanup_workspaces handler exists in HANDLER_REGISTRY."""
        assert "scip_cleanup_workspaces" in HANDLER_REGISTRY

    def test_scip_cleanup_status_handler_exists(self):
        """scip_cleanup_status handler exists in HANDLER_REGISTRY."""
        assert "scip_cleanup_status" in HANDLER_REGISTRY


# =============================================================================
# Tool Schema Validation Tests
# =============================================================================


class TestAdminOpsMCPToolSchemaValidation:
    """Verify all 8 admin operations MCP tools have valid JSON schemas."""

    def test_all_tools_have_name_field(self):
        """All tools have a name field matching the registry key."""
        tools = [
            "query_audit_logs",
            "enter_maintenance_mode",
            "exit_maintenance_mode",
            "get_maintenance_status",
            "scip_pr_history",
            "scip_cleanup_history",
            "scip_cleanup_workspaces",
            "scip_cleanup_status",
        ]
        for tool_name in tools:
            assert tool_name in TOOL_REGISTRY
            assert TOOL_REGISTRY[tool_name]["name"] == tool_name

    def test_all_tools_have_description(self):
        """All tools have a description field."""
        tools = [
            "query_audit_logs",
            "enter_maintenance_mode",
            "exit_maintenance_mode",
            "get_maintenance_status",
            "scip_pr_history",
            "scip_cleanup_history",
            "scip_cleanup_workspaces",
            "scip_cleanup_status",
        ]
        for tool_name in tools:
            assert "description" in TOOL_REGISTRY[tool_name]
            assert len(TOOL_REGISTRY[tool_name]["description"]) > 0

    def test_all_tools_have_input_schema(self):
        """All tools have an inputSchema field."""
        tools = [
            "query_audit_logs",
            "enter_maintenance_mode",
            "exit_maintenance_mode",
            "get_maintenance_status",
            "scip_pr_history",
            "scip_cleanup_history",
            "scip_cleanup_workspaces",
            "scip_cleanup_status",
        ]
        for tool_name in tools:
            assert "inputSchema" in TOOL_REGISTRY[tool_name]
            schema = TOOL_REGISTRY[tool_name]["inputSchema"]
            assert "type" in schema
            assert schema["type"] == "object"

    def test_all_tools_have_required_permission(self):
        """All tools have a required_permission field."""
        tools = [
            "query_audit_logs",
            "enter_maintenance_mode",
            "exit_maintenance_mode",
            "get_maintenance_status",
            "scip_pr_history",
            "scip_cleanup_history",
            "scip_cleanup_workspaces",
            "scip_cleanup_status",
        ]
        for tool_name in tools:
            assert "required_permission" in TOOL_REGISTRY[tool_name]
            perm = TOOL_REGISTRY[tool_name]["required_permission"]
            # Permission must be one of the valid permissions
            assert perm in [
                "query_repos",
                "manage_users",
                "activate_repos",
                "repository:read",
                "repository:write",
            ]
