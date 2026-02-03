"""
Unit tests for Credential Management MCP Tools - Admin Handler Tests.

Story #743: Implement 10 new MCP tools for API key and MCP credential management.

TDD Approach: These tests are written FIRST before implementation.

Tests verify admin credential handler behavior with mocked dependencies.
"""

import pytest
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

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


# =============================================================================
# Handler Functional Tests - admin_list_user_mcp_credentials
# =============================================================================


class TestAdminListUserMCPCredentialsHandler:
    """Tests for admin_list_user_mcp_credentials handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.get_credentials.return_value = [
            {
                "credential_id": "cred-123",
                "client_id_prefix": "mcp_abcd",
                "name": "User's Credential",
                "created_at": "2024-01-01T00:00:00Z",
            }
        ]
        return manager

    def test_admin_list_user_mcp_credentials_returns_success(
        self, admin_user, mock_mcp_credential_manager
    ):
        """admin_list_user_mcp_credentials returns success for admin user."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_list_user_mcp_credentials"]
            result = handler({"username": "target_user"}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_admin_list_user_mcp_credentials_requires_username(self, admin_user):
        """admin_list_user_mcp_credentials fails when username missing."""
        handler = HANDLER_REGISTRY["admin_list_user_mcp_credentials"]
        result = handler({}, admin_user)

        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False
        assert "error" in content


# =============================================================================
# Handler Functional Tests - admin_create_user_mcp_credential
# =============================================================================


class TestAdminCreateUserMCPCredentialHandler:
    """Tests for admin_create_user_mcp_credential handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.generate_credential.return_value = {
            "credential_id": "cred-admin-123",
            "client_id": "mcp_admin_client_id",
            "client_secret": "mcp_sec_admin_secret_value",
            "client_id_prefix": "mcp_adm",
            "name": "Admin Created",
            "created_at": "2024-01-01T00:00:00Z",
        }
        return manager

    def test_admin_create_user_mcp_credential_returns_success(
        self, admin_user, mock_mcp_credential_manager
    ):
        """admin_create_user_mcp_credential returns success for admin user."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_create_user_mcp_credential"]
            result = handler(
                {"username": "target_user", "description": "Admin created"}, admin_user
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_admin_create_user_mcp_credential_returns_credential(
        self, admin_user, mock_mcp_credential_manager
    ):
        """admin_create_user_mcp_credential returns full credential."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_create_user_mcp_credential"]
            result = handler({"username": "target_user"}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert "credential_id" in content


# =============================================================================
# Handler Functional Tests - admin_delete_user_mcp_credential
# =============================================================================


class TestAdminDeleteUserMCPCredentialHandler:
    """Tests for admin_delete_user_mcp_credential handler functionality."""

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.revoke_credential.return_value = True
        return manager

    def test_admin_delete_user_mcp_credential_returns_success(
        self, admin_user, mock_mcp_credential_manager
    ):
        """admin_delete_user_mcp_credential returns success for admin user."""
        with patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps:
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_delete_user_mcp_credential"]
            result = handler(
                {"username": "target_user", "credential_id": "cred-123"}, admin_user
            )

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_admin_delete_user_mcp_credential_requires_both_params(self, admin_user):
        """admin_delete_user_mcp_credential fails when params missing."""
        handler = HANDLER_REGISTRY["admin_delete_user_mcp_credential"]

        # Missing credential_id
        result = handler({"username": "target_user"}, admin_user)
        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False

        # Missing username
        result = handler({"credential_id": "cred-123"}, admin_user)
        content = json.loads(result["content"][0]["text"])
        assert content["success"] is False


# =============================================================================
# Handler Functional Tests - admin_list_all_mcp_credentials
# =============================================================================


class TestAdminListAllMCPCredentialsHandler:
    """Tests for admin_list_all_mcp_credentials handler functionality."""

    @pytest.fixture
    def mock_user_manager(self):
        """Create mock user manager for testing."""
        manager = MagicMock()
        user1 = MagicMock()
        user1.username = "user1"
        user2 = MagicMock()
        user2.username = "user2"
        manager.get_all_users.return_value = [user1, user2]
        return manager

    @pytest.fixture
    def mock_mcp_credential_manager(self):
        """Create mock MCP credential manager for testing."""
        manager = MagicMock()
        manager.get_credentials.side_effect = [
            [
                {
                    "credential_id": "cred-1",
                    "client_id_prefix": "mcp_abc",
                    "name": "User1 Cred",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ],
            [
                {
                    "credential_id": "cred-2",
                    "client_id_prefix": "mcp_xyz",
                    "name": "User2 Cred",
                    "created_at": "2024-01-02T00:00:00Z",
                }
            ],
        ]
        return manager

    def test_admin_list_all_mcp_credentials_returns_success(
        self, admin_user, mock_user_manager, mock_mcp_credential_manager
    ):
        """admin_list_all_mcp_credentials returns success for admin user."""
        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_app.user_manager = mock_user_manager
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_list_all_mcp_credentials"]
            result = handler({}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert content["success"] is True

    def test_admin_list_all_mcp_credentials_returns_credentials_array(
        self, admin_user, mock_user_manager, mock_mcp_credential_manager
    ):
        """admin_list_all_mcp_credentials returns credentials array."""
        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_app.user_manager = mock_user_manager
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_list_all_mcp_credentials"]
            result = handler({}, admin_user)

            content = json.loads(result["content"][0]["text"])
            assert "credentials" in content
            assert isinstance(content["credentials"], list)

    def test_admin_list_all_mcp_credentials_includes_username(
        self, admin_user, mock_user_manager, mock_mcp_credential_manager
    ):
        """admin_list_all_mcp_credentials includes username in each credential."""
        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch("code_indexer.server.mcp.handlers.dependencies") as mock_deps,
        ):
            mock_app.user_manager = mock_user_manager
            mock_deps.mcp_credential_manager = mock_mcp_credential_manager

            handler = HANDLER_REGISTRY["admin_list_all_mcp_credentials"]
            result = handler({}, admin_user)

            content = json.loads(result["content"][0]["text"])
            # Each credential should have a username field
            for cred in content["credentials"]:
                assert "username" in cred
