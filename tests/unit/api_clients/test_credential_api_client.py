"""
Tests for CredentialAPIClient method signatures and initialization.

Following Foundation #1 compliance: Zero mocks, real server testing.
Story #748: Credential Management from CLI Remote Mode

This file contains tests for:
- CredentialAPIClient class existence and method signatures
- CredentialAPIClient initialization
- All 10 methods required by the story
"""

import pytest
import tempfile
from pathlib import Path
from typing import Dict, Any


class TestCredentialAPIClientMethods:
    """Test CredentialAPIClient method signatures and structure."""

    def test_credential_api_client_class_exists(self):
        """Test CredentialAPIClient class exists and can be imported."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert CredentialAPIClient is not None

    # =============================================================================
    # API Key Methods (3 methods)
    # =============================================================================

    def test_credential_api_client_has_list_api_keys_method(self):
        """Test CredentialAPIClient has list_api_keys method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "list_api_keys")
        assert callable(getattr(CredentialAPIClient, "list_api_keys"))

    def test_credential_api_client_has_create_api_key_method(self):
        """Test CredentialAPIClient has create_api_key method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "create_api_key")
        assert callable(getattr(CredentialAPIClient, "create_api_key"))

    def test_credential_api_client_has_delete_api_key_method(self):
        """Test CredentialAPIClient has delete_api_key method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "delete_api_key")
        assert callable(getattr(CredentialAPIClient, "delete_api_key"))

    # =============================================================================
    # MCP Credential Methods - User Self-Service (3 methods)
    # =============================================================================

    def test_credential_api_client_has_list_mcp_credentials_method(self):
        """Test CredentialAPIClient has list_mcp_credentials method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "list_mcp_credentials")
        assert callable(getattr(CredentialAPIClient, "list_mcp_credentials"))

    def test_credential_api_client_has_create_mcp_credential_method(self):
        """Test CredentialAPIClient has create_mcp_credential method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "create_mcp_credential")
        assert callable(getattr(CredentialAPIClient, "create_mcp_credential"))

    def test_credential_api_client_has_delete_mcp_credential_method(self):
        """Test CredentialAPIClient has delete_mcp_credential method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "delete_mcp_credential")
        assert callable(getattr(CredentialAPIClient, "delete_mcp_credential"))

    # =============================================================================
    # Admin MCP Credential Methods (4 methods)
    # =============================================================================

    def test_credential_api_client_has_admin_list_user_mcp_credentials_method(self):
        """Test CredentialAPIClient has admin_list_user_mcp_credentials method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "admin_list_user_mcp_credentials")
        assert callable(getattr(CredentialAPIClient, "admin_list_user_mcp_credentials"))

    def test_credential_api_client_has_admin_create_user_mcp_credential_method(self):
        """Test CredentialAPIClient has admin_create_user_mcp_credential method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "admin_create_user_mcp_credential")
        assert callable(
            getattr(CredentialAPIClient, "admin_create_user_mcp_credential")
        )

    def test_credential_api_client_has_admin_delete_user_mcp_credential_method(self):
        """Test CredentialAPIClient has admin_delete_user_mcp_credential method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "admin_delete_user_mcp_credential")
        assert callable(
            getattr(CredentialAPIClient, "admin_delete_user_mcp_credential")
        )

    def test_credential_api_client_has_admin_list_all_mcp_credentials_method(self):
        """Test CredentialAPIClient has admin_list_all_mcp_credentials method."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        assert hasattr(CredentialAPIClient, "admin_list_all_mcp_credentials")
        assert callable(getattr(CredentialAPIClient, "admin_list_all_mcp_credentials"))

    # =============================================================================
    # Inheritance Tests
    # =============================================================================

    def test_credential_api_client_inherits_from_base_client(self):
        """Test CredentialAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(CredentialAPIClient, CIDXRemoteAPIClient)


class TestCredentialAPIClientInitialization:
    """Test CredentialAPIClient initialization."""

    @pytest.fixture
    def user_credentials(self) -> Dict[str, Any]:
        """User credentials for testing."""
        return {
            "username": "testuser",
            "password": "testpass123",
        }

    @pytest.fixture
    def temp_project_root(self):
        """Create temporary project root for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    def test_credential_client_initialization(
        self, user_credentials, temp_project_root
    ):
        """Test CredentialAPIClient initialization."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        credential_client = CredentialAPIClient(
            server_url="https://test.example.com",
            credentials=user_credentials,
            project_root=temp_project_root,
        )

        assert credential_client.server_url == "https://test.example.com"
        assert credential_client.credentials == user_credentials
        assert credential_client.project_root == temp_project_root

    def test_credential_client_initialization_without_project_root(
        self, user_credentials
    ):
        """Test CredentialAPIClient initialization without project root."""
        from code_indexer.api_clients.credential_client import CredentialAPIClient

        credential_client = CredentialAPIClient(
            server_url="https://test.example.com",
            credentials=user_credentials,
            project_root=None,
        )

        assert credential_client.server_url == "https://test.example.com"
        assert credential_client.credentials == user_credentials
        assert credential_client.project_root is None


class TestCredentialAPIClientExportsFromModule:
    """Test CredentialAPIClient is exported from api_clients module."""

    def test_credential_api_client_in_module_exports(self):
        """Test CredentialAPIClient is exported from api_clients module."""
        from code_indexer.api_clients import CredentialAPIClient

        assert CredentialAPIClient is not None
