"""
Tests for GroupAPIClient method signatures and initialization.

Following Foundation #1 compliance: Zero mocks, real server testing.
Story #747: Group & Access Management from CLI Remote Mode

This file contains tests for:
- GroupAPIClient class existence and method signatures
- GroupAPIClient initialization
"""

import pytest
import tempfile
from pathlib import Path
from typing import Dict, Any


class TestGroupAPIClientMethods:
    """Test GroupAPIClient method signatures and structure."""

    def test_group_api_client_class_exists(self):
        """Test GroupAPIClient class exists and can be imported."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert GroupAPIClient is not None

    def test_group_api_client_has_list_groups_method(self):
        """Test GroupAPIClient has list_groups method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "list_groups")
        assert callable(getattr(GroupAPIClient, "list_groups"))

    def test_group_api_client_has_create_group_method(self):
        """Test GroupAPIClient has create_group method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "create_group")
        assert callable(getattr(GroupAPIClient, "create_group"))

    def test_group_api_client_has_get_group_method(self):
        """Test GroupAPIClient has get_group method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "get_group")
        assert callable(getattr(GroupAPIClient, "get_group"))

    def test_group_api_client_has_update_group_method(self):
        """Test GroupAPIClient has update_group method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "update_group")
        assert callable(getattr(GroupAPIClient, "update_group"))

    def test_group_api_client_has_delete_group_method(self):
        """Test GroupAPIClient has delete_group method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "delete_group")
        assert callable(getattr(GroupAPIClient, "delete_group"))

    def test_group_api_client_has_add_member_method(self):
        """Test GroupAPIClient has add_member method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "add_member")
        assert callable(getattr(GroupAPIClient, "add_member"))

    def test_group_api_client_has_add_repos_method(self):
        """Test GroupAPIClient has add_repos method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "add_repos")
        assert callable(getattr(GroupAPIClient, "add_repos"))

    def test_group_api_client_has_remove_repo_method(self):
        """Test GroupAPIClient has remove_repo method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "remove_repo")
        assert callable(getattr(GroupAPIClient, "remove_repo"))

    def test_group_api_client_has_remove_repos_method(self):
        """Test GroupAPIClient has remove_repos method."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        assert hasattr(GroupAPIClient, "remove_repos")
        assert callable(getattr(GroupAPIClient, "remove_repos"))

    def test_group_api_client_inherits_from_base_client(self):
        """Test GroupAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.group_client import GroupAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(GroupAPIClient, CIDXRemoteAPIClient)


class TestGroupAPIClientInitialization:
    """Test GroupAPIClient initialization."""

    @pytest.fixture
    def admin_credentials(self) -> Dict[str, Any]:
        """Admin credentials for testing."""
        return {
            "username": "admin",
            "password": "admin123",
        }

    @pytest.fixture
    def temp_project_root(self):
        """Create temporary project root for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    def test_group_client_initialization(self, admin_credentials, temp_project_root):
        """Test GroupAPIClient initialization."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        group_client = GroupAPIClient(
            server_url="https://test.example.com",
            credentials=admin_credentials,
            project_root=temp_project_root,
        )

        assert group_client.server_url == "https://test.example.com"
        assert group_client.credentials == admin_credentials
        assert group_client.project_root == temp_project_root

    def test_group_client_initialization_without_project_root(self, admin_credentials):
        """Test GroupAPIClient initialization without project root."""
        from code_indexer.api_clients.group_client import GroupAPIClient

        group_client = GroupAPIClient(
            server_url="https://test.example.com",
            credentials=admin_credentials,
            project_root=None,
        )

        assert group_client.server_url == "https://test.example.com"
        assert group_client.credentials == admin_credentials
        assert group_client.project_root is None
