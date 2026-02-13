"""
Tests for field stripping in list_repositories handler (Story #196).

Tests verify that only MCP-relevant fields are returned in list_repositories response.
"""

import json
import pytest
from unittest.mock import Mock, patch

from code_indexer.server.mcp.handlers import list_repositories
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


class TestListRepositoriesFieldStripping:
    """Test list_repositories handler field stripping (Story #196)."""

    def test_activated_repo_fields_are_filtered(self, mock_user):
        """AC1: Test that activated repos only include whitelisted fields."""
        # Simulate raw metadata from activated_repo_manager with all possible fields
        mock_activated_repos = [
            {
                "user_alias": "my-repo",
                "golden_repo_alias": "backend-api",
                "current_branch": "main",
                "activated_at": "2024-01-15T10:30:00",
                "last_accessed": "2024-01-15T10:30:00",
                "git_committer_email": "user@example.com",
                "ssh_key_used": "~/.ssh/id_rsa",
                "index_path": "/home/user/.cidx-server/repos/testuser/my-repo",
                "username": "testuser",
                "path": "/home/testuser/projects/backend-api",
            }
        ]

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=mock_activated_repos)
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=[])
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service (no categories)
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value={})
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify success
            assert data["success"] is True
            assert len(data["repositories"]) == 1

            repo = data["repositories"][0]

            # AC1: Verify ONLY whitelisted fields are present
            # Fields that SHOULD be present (whitelist)
            assert "user_alias" in repo
            assert "golden_repo_alias" in repo
            assert "current_branch" in repo
            assert "repo_category" in repo  # Added by category enrichment

            # Fields that should NOT be present (stripped)
            assert "index_path" not in repo
            assert "username" not in repo
            assert "path" not in repo
            assert "git_committer_email" not in repo
            assert "ssh_key_used" not in repo
            assert "last_accessed" not in repo
            assert "activated_at" not in repo

            # Verify values of kept fields
            assert repo["user_alias"] == "my-repo"
            assert repo["golden_repo_alias"] == "backend-api"
            assert repo["current_branch"] == "main"

    def test_global_repo_index_path_removed(self, mock_user):
        """AC2: Test that global repos don't include index_path field."""
        mock_global_repos = [
            {
                "alias_name": "backend-api-global",
                "repo_name": "backend-api",
                "repo_url": "https://github.com/org/backend-api.git",
                "last_refresh": "2024-01-15T10:30:00",
                "index_path": "/var/cidx-server/global-repos/backend-api/.code-indexer/index",
                "created_at": "2024-01-10T08:00:00",
            }
        ]

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=[])
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=mock_global_repos)
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service (no categories)
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value={})
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify success
            assert data["success"] is True
            assert len(data["repositories"]) == 1

            repo = data["repositories"][0]

            # AC2: Verify index_path is NOT present
            assert "index_path" not in repo

            # Verify other global repo fields are present
            assert repo["user_alias"] == "backend-api-global"
            assert repo["golden_repo_alias"] == "backend-api"
            assert repo["repo_url"] == "https://github.com/org/backend-api.git"
            assert repo["last_refresh"] == "2024-01-15T10:30:00"
            assert repo["is_global"] is True

    def test_composite_repo_fields_filtered(self, mock_user):
        """AC5: Test that composite repos include golden_repo_aliases but not discovered_repos."""
        mock_activated_repos = [
            {
                "user_alias": "my-composite",
                "username": "testuser",
                "path": "/home/testuser/.cidx-server/composite/my-composite",
                "is_composite": True,
                "golden_repo_aliases": ["backend-api", "frontend-app", "ml-model"],
                "discovered_repos": ["backend-api", "frontend-app", "ml-model"],  # Duplicate
                "activated_at": "2024-01-15T10:30:00",
                "last_accessed": "2024-01-15T10:30:00",
            }
        ]

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=mock_activated_repos)
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=[])
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service (no categories)
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value={})
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify success
            assert data["success"] is True
            assert len(data["repositories"]) == 1

            repo = data["repositories"][0]

            # AC5: Verify composite-specific fields
            assert "is_composite" in repo
            assert repo["is_composite"] is True
            assert "golden_repo_aliases" in repo
            assert repo["golden_repo_aliases"] == ["backend-api", "frontend-app", "ml-model"]

            # Verify discovered_repos is NOT present (it's a duplicate)
            assert "discovered_repos" not in repo

            # Verify other unnecessary fields are stripped
            assert "username" not in repo
            assert "path" not in repo
            assert "activated_at" not in repo
            assert "last_accessed" not in repo

    def test_backward_compatibility_response_structure(self, mock_user):
        """AC3: Test that response structure remains compatible."""
        mock_activated_repos = [
            {"user_alias": "repo1", "golden_repo_alias": "golden1", "current_branch": "main"}
        ]

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=mock_activated_repos)
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=[])
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value={})
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # AC3: Verify response structure is unchanged
            assert "success" in data
            assert "repositories" in data
            assert data["success"] is True
            assert isinstance(data["repositories"], list)

    def test_category_filter_still_works(self, mock_user):
        """AC3: Test that category filtering parameter still works after field stripping."""
        mock_activated_repos = [
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "current_branch": "main"},
            {"user_alias": "frontend-app", "golden_repo_alias": "frontend-app", "current_branch": "main"},
        ]

        mock_category_map = {
            "backend-api": {"category_name": "Backend", "priority": 1},
            "frontend-app": {"category_name": "Frontend", "priority": 2},
        }

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=mock_activated_repos)
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=[])
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value=mock_category_map)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute with category filter
            result = list_repositories({"category": "Backend"}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # AC3: Verify category filter still works
            assert data["success"] is True
            assert len(data["repositories"]) == 1
            assert data["repositories"][0]["user_alias"] == "backend-api"
            assert data["repositories"][0]["repo_category"] == "Backend"

    def test_kept_fields_maintain_types_and_semantics(self, mock_user):
        """AC3: Test that kept fields maintain their original types and semantics."""
        mock_activated_repos = [
            {
                "user_alias": "my-repo",
                "golden_repo_alias": "backend-api",
                "current_branch": "feature/new-feature",
                "is_global": False,
            }
        ]

        mock_global_repos = [
            {
                "alias_name": "shared-global",
                "repo_name": "shared-repo",
                "repo_url": "https://example.com/repo.git",
                "last_refresh": "2024-01-15T10:30:00",
            }
        ]

        with (
            patch("code_indexer.server.app.activated_repo_manager") as mock_repo_manager,
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_get_dir,
            patch("code_indexer.server.mcp.handlers.get_server_global_registry") as mock_get_registry,
            patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager,
        ):
            # Setup mocks
            mock_repo_manager.list_activated_repositories = Mock(return_value=mock_activated_repos)
            mock_get_dir.return_value = "/mock/golden-repos"

            mock_registry_instance = Mock()
            mock_registry_instance.list_global_repos = Mock(return_value=mock_global_repos)
            mock_get_registry.return_value = mock_registry_instance

            # Mock category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value={})
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # AC3: Verify field types and semantics are preserved
            activated_repo = next(r for r in data["repositories"] if r["user_alias"] == "my-repo")
            assert isinstance(activated_repo["user_alias"], str)
            assert isinstance(activated_repo["golden_repo_alias"], str)
            assert isinstance(activated_repo["current_branch"], str)
            assert activated_repo["current_branch"] == "feature/new-feature"

            global_repo = next(r for r in data["repositories"] if r["user_alias"] == "shared-global")
            assert isinstance(global_repo["is_global"], bool)
            assert global_repo["is_global"] is True
            assert isinstance(global_repo["repo_url"], str)
            assert isinstance(global_repo["last_refresh"], str)
            assert global_repo["current_branch"] is None  # Global repos have None for current_branch
