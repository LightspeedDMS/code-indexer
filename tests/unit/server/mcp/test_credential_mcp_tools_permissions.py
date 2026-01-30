"""
Unit tests for Credential Management MCP Tools - Permission and Schema Tests.

Story #743: Implement 10 new MCP tools for API key and MCP credential management.

TDD Approach: These tests are written FIRST before implementation.

Tests verify:
1. Permission requirements are set correctly (query_repos vs manage_users)
2. Input schema validation for each tool
"""


from code_indexer.server.mcp.tools import TOOL_REGISTRY


# =============================================================================
# Permission Requirements Tests - User Self-Service (query_repos)
# =============================================================================


class TestUserSelfServiceToolsPermissions:
    """Verify user self-service tools require query_repos permission."""

    # API Keys (3)
    def test_list_api_keys_requires_query_repos_permission(self):
        """list_api_keys tool requires query_repos permission."""
        assert TOOL_REGISTRY["list_api_keys"]["required_permission"] == "query_repos"

    def test_create_api_key_requires_query_repos_permission(self):
        """create_api_key tool requires query_repos permission."""
        assert TOOL_REGISTRY["create_api_key"]["required_permission"] == "query_repos"

    def test_delete_api_key_requires_query_repos_permission(self):
        """delete_api_key tool requires query_repos permission."""
        assert TOOL_REGISTRY["delete_api_key"]["required_permission"] == "query_repos"

    # MCP Credentials (3)
    def test_list_mcp_credentials_requires_query_repos_permission(self):
        """list_mcp_credentials tool requires query_repos permission."""
        assert (
            TOOL_REGISTRY["list_mcp_credentials"]["required_permission"] == "query_repos"
        )

    def test_create_mcp_credential_requires_query_repos_permission(self):
        """create_mcp_credential tool requires query_repos permission."""
        assert (
            TOOL_REGISTRY["create_mcp_credential"]["required_permission"] == "query_repos"
        )

    def test_delete_mcp_credential_requires_query_repos_permission(self):
        """delete_mcp_credential tool requires query_repos permission."""
        assert (
            TOOL_REGISTRY["delete_mcp_credential"]["required_permission"] == "query_repos"
        )


# =============================================================================
# Permission Requirements Tests - Admin Operations (manage_users)
# =============================================================================


class TestAdminToolsPermissions:
    """Verify admin tools require manage_users permission."""

    def test_admin_list_user_mcp_credentials_requires_manage_users_permission(self):
        """admin_list_user_mcp_credentials requires manage_users permission."""
        assert (
            TOOL_REGISTRY["admin_list_user_mcp_credentials"]["required_permission"]
            == "manage_users"
        )

    def test_admin_create_user_mcp_credential_requires_manage_users_permission(self):
        """admin_create_user_mcp_credential requires manage_users permission."""
        assert (
            TOOL_REGISTRY["admin_create_user_mcp_credential"]["required_permission"]
            == "manage_users"
        )

    def test_admin_delete_user_mcp_credential_requires_manage_users_permission(self):
        """admin_delete_user_mcp_credential requires manage_users permission."""
        assert (
            TOOL_REGISTRY["admin_delete_user_mcp_credential"]["required_permission"]
            == "manage_users"
        )

    def test_admin_list_all_mcp_credentials_requires_manage_users_permission(self):
        """admin_list_all_mcp_credentials requires manage_users permission."""
        assert (
            TOOL_REGISTRY["admin_list_all_mcp_credentials"]["required_permission"]
            == "manage_users"
        )


# =============================================================================
# Input Schema Validation Tests - User Self-Service API Keys
# =============================================================================


class TestListAPIKeysSchema:
    """Tests for list_api_keys tool schema."""

    def test_list_api_keys_has_input_schema(self):
        """list_api_keys tool has inputSchema defined."""
        schema = TOOL_REGISTRY["list_api_keys"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_list_api_keys_has_no_required_properties(self):
        """list_api_keys requires no inputs."""
        schema = TOOL_REGISTRY["list_api_keys"]["inputSchema"]
        assert schema.get("required", []) == []

    def test_list_api_keys_has_description(self):
        """list_api_keys has a meaningful description."""
        tool = TOOL_REGISTRY["list_api_keys"]
        assert "description" in tool
        assert len(tool["description"]) > 10


class TestCreateAPIKeySchema:
    """Tests for create_api_key tool schema."""

    def test_create_api_key_has_input_schema(self):
        """create_api_key tool has inputSchema defined."""
        schema = TOOL_REGISTRY["create_api_key"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_create_api_key_description_is_optional(self):
        """create_api_key description parameter is optional."""
        schema = TOOL_REGISTRY["create_api_key"]["inputSchema"]
        assert "description" in schema["properties"]
        assert "description" not in schema.get("required", [])


class TestDeleteAPIKeySchema:
    """Tests for delete_api_key tool schema."""

    def test_delete_api_key_has_input_schema(self):
        """delete_api_key tool has inputSchema defined."""
        schema = TOOL_REGISTRY["delete_api_key"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_delete_api_key_requires_key_id(self):
        """delete_api_key requires key_id parameter."""
        schema = TOOL_REGISTRY["delete_api_key"]["inputSchema"]
        assert "key_id" in schema["properties"]
        assert "key_id" in schema.get("required", [])

    def test_delete_api_key_key_id_is_string(self):
        """delete_api_key key_id parameter is a string."""
        schema = TOOL_REGISTRY["delete_api_key"]["inputSchema"]
        assert schema["properties"]["key_id"]["type"] == "string"


# =============================================================================
# Input Schema Validation Tests - User Self-Service MCP Credentials
# =============================================================================


class TestListMCPCredentialsSchema:
    """Tests for list_mcp_credentials tool schema."""

    def test_list_mcp_credentials_has_input_schema(self):
        """list_mcp_credentials tool has inputSchema defined."""
        schema = TOOL_REGISTRY["list_mcp_credentials"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_list_mcp_credentials_has_no_required_properties(self):
        """list_mcp_credentials requires no inputs."""
        schema = TOOL_REGISTRY["list_mcp_credentials"]["inputSchema"]
        assert schema.get("required", []) == []


class TestCreateMCPCredentialSchema:
    """Tests for create_mcp_credential tool schema."""

    def test_create_mcp_credential_has_input_schema(self):
        """create_mcp_credential tool has inputSchema defined."""
        schema = TOOL_REGISTRY["create_mcp_credential"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_create_mcp_credential_description_is_optional(self):
        """create_mcp_credential description parameter is optional."""
        schema = TOOL_REGISTRY["create_mcp_credential"]["inputSchema"]
        assert "description" in schema["properties"]
        assert "description" not in schema.get("required", [])


class TestDeleteMCPCredentialSchema:
    """Tests for delete_mcp_credential tool schema."""

    def test_delete_mcp_credential_has_input_schema(self):
        """delete_mcp_credential tool has inputSchema defined."""
        schema = TOOL_REGISTRY["delete_mcp_credential"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_delete_mcp_credential_requires_credential_id(self):
        """delete_mcp_credential requires credential_id parameter."""
        schema = TOOL_REGISTRY["delete_mcp_credential"]["inputSchema"]
        assert "credential_id" in schema["properties"]
        assert "credential_id" in schema.get("required", [])

    def test_delete_mcp_credential_credential_id_is_string(self):
        """delete_mcp_credential credential_id parameter is a string."""
        schema = TOOL_REGISTRY["delete_mcp_credential"]["inputSchema"]
        assert schema["properties"]["credential_id"]["type"] == "string"


# =============================================================================
# Input Schema Validation Tests - Admin Operations
# =============================================================================


class TestAdminListUserMCPCredentialsSchema:
    """Tests for admin_list_user_mcp_credentials tool schema."""

    def test_admin_list_user_mcp_credentials_has_input_schema(self):
        """admin_list_user_mcp_credentials tool has inputSchema defined."""
        schema = TOOL_REGISTRY["admin_list_user_mcp_credentials"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_admin_list_user_mcp_credentials_requires_username(self):
        """admin_list_user_mcp_credentials requires username parameter."""
        schema = TOOL_REGISTRY["admin_list_user_mcp_credentials"]["inputSchema"]
        assert "username" in schema["properties"]
        assert "username" in schema.get("required", [])


class TestAdminCreateUserMCPCredentialSchema:
    """Tests for admin_create_user_mcp_credential tool schema."""

    def test_admin_create_user_mcp_credential_has_input_schema(self):
        """admin_create_user_mcp_credential tool has inputSchema defined."""
        schema = TOOL_REGISTRY["admin_create_user_mcp_credential"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_admin_create_user_mcp_credential_requires_username(self):
        """admin_create_user_mcp_credential requires username parameter."""
        schema = TOOL_REGISTRY["admin_create_user_mcp_credential"]["inputSchema"]
        assert "username" in schema["properties"]
        assert "username" in schema.get("required", [])

    def test_admin_create_user_mcp_credential_description_is_optional(self):
        """admin_create_user_mcp_credential description parameter is optional."""
        schema = TOOL_REGISTRY["admin_create_user_mcp_credential"]["inputSchema"]
        assert "description" in schema["properties"]
        assert "description" not in schema.get("required", [])


class TestAdminDeleteUserMCPCredentialSchema:
    """Tests for admin_delete_user_mcp_credential tool schema."""

    def test_admin_delete_user_mcp_credential_has_input_schema(self):
        """admin_delete_user_mcp_credential tool has inputSchema defined."""
        schema = TOOL_REGISTRY["admin_delete_user_mcp_credential"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_admin_delete_user_mcp_credential_requires_username(self):
        """admin_delete_user_mcp_credential requires username parameter."""
        schema = TOOL_REGISTRY["admin_delete_user_mcp_credential"]["inputSchema"]
        assert "username" in schema["properties"]
        assert "username" in schema.get("required", [])

    def test_admin_delete_user_mcp_credential_requires_credential_id(self):
        """admin_delete_user_mcp_credential requires credential_id parameter."""
        schema = TOOL_REGISTRY["admin_delete_user_mcp_credential"]["inputSchema"]
        assert "credential_id" in schema["properties"]
        assert "credential_id" in schema.get("required", [])


class TestAdminListAllMCPCredentialsSchema:
    """Tests for admin_list_all_mcp_credentials tool schema."""

    def test_admin_list_all_mcp_credentials_has_input_schema(self):
        """admin_list_all_mcp_credentials tool has inputSchema defined."""
        schema = TOOL_REGISTRY["admin_list_all_mcp_credentials"]
        assert "inputSchema" in schema
        assert schema["inputSchema"]["type"] == "object"

    def test_admin_list_all_mcp_credentials_has_no_required_properties(self):
        """admin_list_all_mcp_credentials requires no inputs."""
        schema = TOOL_REGISTRY["admin_list_all_mcp_credentials"]["inputSchema"]
        assert schema.get("required", []) == []
