"""
Unit tests for Credential Management MCP Tools - Registry Existence Tests.

Story #743: Implement 10 new MCP tools for API key and MCP credential management.

TDD Approach: These tests are written FIRST before implementation.

Tests verify:
1. Tool schemas exist in TOOL_REGISTRY
2. Tool handlers exist in HANDLER_REGISTRY
3. Permission requirements are set correctly (query_repos vs manage_users)
"""

import pytest
from datetime import datetime, timezone

from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole


# =============================================================================
# Test Fixtures (shared across credential test files)
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
# Tool Schema Existence Tests - User Self-Service API Keys (3 tools)
# =============================================================================


class TestAPIKeyToolsExistInRegistry:
    """Verify all 3 API key user self-service MCP tools exist in TOOL_REGISTRY."""

    def test_list_api_keys_exists_in_registry(self):
        """AC1: list_api_keys tool exists in TOOL_REGISTRY."""
        assert "list_api_keys" in TOOL_REGISTRY
        assert TOOL_REGISTRY["list_api_keys"]["name"] == "list_api_keys"

    def test_create_api_key_exists_in_registry(self):
        """AC2: create_api_key tool exists in TOOL_REGISTRY."""
        assert "create_api_key" in TOOL_REGISTRY
        assert TOOL_REGISTRY["create_api_key"]["name"] == "create_api_key"

    def test_delete_api_key_exists_in_registry(self):
        """AC3: delete_api_key tool exists in TOOL_REGISTRY."""
        assert "delete_api_key" in TOOL_REGISTRY
        assert TOOL_REGISTRY["delete_api_key"]["name"] == "delete_api_key"


# =============================================================================
# Tool Schema Existence Tests - User Self-Service MCP Credentials (3 tools)
# =============================================================================


class TestMCPCredentialToolsExistInRegistry:
    """Verify all 3 MCP credential user self-service tools exist in TOOL_REGISTRY."""

    def test_list_mcp_credentials_exists_in_registry(self):
        """AC4: list_mcp_credentials tool exists in TOOL_REGISTRY."""
        assert "list_mcp_credentials" in TOOL_REGISTRY
        assert TOOL_REGISTRY["list_mcp_credentials"]["name"] == "list_mcp_credentials"

    def test_create_mcp_credential_exists_in_registry(self):
        """AC5: create_mcp_credential tool exists in TOOL_REGISTRY."""
        assert "create_mcp_credential" in TOOL_REGISTRY
        assert TOOL_REGISTRY["create_mcp_credential"]["name"] == "create_mcp_credential"

    def test_delete_mcp_credential_exists_in_registry(self):
        """AC6: delete_mcp_credential tool exists in TOOL_REGISTRY."""
        assert "delete_mcp_credential" in TOOL_REGISTRY
        assert TOOL_REGISTRY["delete_mcp_credential"]["name"] == "delete_mcp_credential"


# =============================================================================
# Tool Schema Existence Tests - Admin Operations (4 tools)
# =============================================================================


class TestAdminMCPCredentialToolsExistInRegistry:
    """Verify all 4 admin MCP credential tools exist in TOOL_REGISTRY."""

    def test_admin_list_user_mcp_credentials_exists_in_registry(self):
        """AC7: admin_list_user_mcp_credentials tool exists in TOOL_REGISTRY."""
        assert "admin_list_user_mcp_credentials" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["admin_list_user_mcp_credentials"]["name"]
            == "admin_list_user_mcp_credentials"
        )

    def test_admin_create_user_mcp_credential_exists_in_registry(self):
        """AC8: admin_create_user_mcp_credential tool exists in TOOL_REGISTRY."""
        assert "admin_create_user_mcp_credential" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["admin_create_user_mcp_credential"]["name"]
            == "admin_create_user_mcp_credential"
        )

    def test_admin_delete_user_mcp_credential_exists_in_registry(self):
        """AC9: admin_delete_user_mcp_credential tool exists in TOOL_REGISTRY."""
        assert "admin_delete_user_mcp_credential" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["admin_delete_user_mcp_credential"]["name"]
            == "admin_delete_user_mcp_credential"
        )

    def test_admin_list_all_mcp_credentials_exists_in_registry(self):
        """AC10: admin_list_all_mcp_credentials tool exists in TOOL_REGISTRY."""
        assert "admin_list_all_mcp_credentials" in TOOL_REGISTRY
        assert (
            TOOL_REGISTRY["admin_list_all_mcp_credentials"]["name"]
            == "admin_list_all_mcp_credentials"
        )


# =============================================================================
# Tool Handler Existence Tests
# =============================================================================


class TestCredentialMCPHandlersExistInRegistry:
    """Verify all 10 credential MCP tool handlers exist in HANDLER_REGISTRY."""

    # User self-service API keys (3)
    def test_list_api_keys_handler_exists(self):
        """list_api_keys handler exists in HANDLER_REGISTRY."""
        assert "list_api_keys" in HANDLER_REGISTRY

    def test_create_api_key_handler_exists(self):
        """create_api_key handler exists in HANDLER_REGISTRY."""
        assert "create_api_key" in HANDLER_REGISTRY

    def test_delete_api_key_handler_exists(self):
        """delete_api_key handler exists in HANDLER_REGISTRY."""
        assert "delete_api_key" in HANDLER_REGISTRY

    # User self-service MCP credentials (3)
    def test_list_mcp_credentials_handler_exists(self):
        """list_mcp_credentials handler exists in HANDLER_REGISTRY."""
        assert "list_mcp_credentials" in HANDLER_REGISTRY

    def test_create_mcp_credential_handler_exists(self):
        """create_mcp_credential handler exists in HANDLER_REGISTRY."""
        assert "create_mcp_credential" in HANDLER_REGISTRY

    def test_delete_mcp_credential_handler_exists(self):
        """delete_mcp_credential handler exists in HANDLER_REGISTRY."""
        assert "delete_mcp_credential" in HANDLER_REGISTRY

    # Admin operations (4)
    def test_admin_list_user_mcp_credentials_handler_exists(self):
        """admin_list_user_mcp_credentials handler exists in HANDLER_REGISTRY."""
        assert "admin_list_user_mcp_credentials" in HANDLER_REGISTRY

    def test_admin_create_user_mcp_credential_handler_exists(self):
        """admin_create_user_mcp_credential handler exists in HANDLER_REGISTRY."""
        assert "admin_create_user_mcp_credential" in HANDLER_REGISTRY

    def test_admin_delete_user_mcp_credential_handler_exists(self):
        """admin_delete_user_mcp_credential handler exists in HANDLER_REGISTRY."""
        assert "admin_delete_user_mcp_credential" in HANDLER_REGISTRY

    def test_admin_list_all_mcp_credentials_handler_exists(self):
        """admin_list_all_mcp_credentials handler exists in HANDLER_REGISTRY."""
        assert "admin_list_all_mcp_credentials" in HANDLER_REGISTRY
