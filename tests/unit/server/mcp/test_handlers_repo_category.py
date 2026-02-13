"""
Tests for repository category MCP handler integration (Story #182).

Tests list_repositories category enrichment and list_repo_categories handler.
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


@pytest.fixture
def mock_admin_user():
    """Create a mock admin user for testing."""
    user = Mock(spec=User)
    user.username = "admin"
    user.role = UserRole.ADMIN
    user.has_permission = Mock(return_value=True)
    return user


@pytest.fixture
def mock_category_map():
    """Mock category map for testing."""
    return {
        "backend-api": {"category_name": "Backend", "priority": 1},
        "frontend-app": {"category_name": "Frontend", "priority": 2},
        "ml-model": {"category_name": "ML/AI", "priority": 3},
        "misc-tool": {"category_name": None, "priority": None},  # Unassigned
    }


@pytest.fixture
def mock_categories():
    """Mock categories list for list_repo_categories handler."""
    return [
        {"id": 1, "name": "Backend", "pattern": ".*-api$", "priority": 1},
        {"id": 2, "name": "Frontend", "pattern": ".*-app$", "priority": 2},
        {"id": 3, "name": "ML/AI", "pattern": "^ml-.*", "priority": 3},
    ]


class TestListRepositoriesCategoryEnrichment:
    """Test list_repositories handler category enrichment (Step 3)."""

    def test_list_repositories_includes_category_field(self, mock_user, mock_category_map):
        """Test that list_repositories includes repo_category field for each repo."""
        mock_activated_repos = [
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "is_global": False},
            {"user_alias": "misc-tool", "golden_repo_alias": "misc-tool", "is_global": False},
        ]

        mock_global_repos = [
            {"alias_name": "frontend-app-global", "repo_name": "frontend-app", "repo_url": "https://example.com"},
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

            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value=mock_category_map)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify
            assert data["success"] is True
            assert len(data["repositories"]) == 3

            # Check activated repo with category
            backend_repo = next(r for r in data["repositories"] if r["user_alias"] == "backend-api")
            assert backend_repo["repo_category"] == "Backend"

            # Check activated repo without category (Unassigned)
            misc_repo = next(r for r in data["repositories"] if r["user_alias"] == "misc-tool")
            assert misc_repo["repo_category"] is None

            # Check global repo with category (inherits from golden_repo_alias)
            global_repo = next(r for r in data["repositories"] if r["user_alias"] == "frontend-app-global")
            assert global_repo["repo_category"] == "Frontend"

    def test_list_repositories_filter_by_category(self, mock_user, mock_category_map):
        """Test list_repositories filtering by category parameter."""
        mock_activated_repos = [
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "is_global": False},
            {"user_alias": "frontend-app", "golden_repo_alias": "frontend-app", "is_global": False},
            {"user_alias": "misc-tool", "golden_repo_alias": "misc-tool", "is_global": False},
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

            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value=mock_category_map)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute with category filter
            result = list_repositories({"category": "Backend"}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify - should only return Backend repos
            assert data["success"] is True
            assert len(data["repositories"]) == 1
            assert data["repositories"][0]["user_alias"] == "backend-api"
            assert data["repositories"][0]["repo_category"] == "Backend"

    def test_list_repositories_filter_unassigned_category(self, mock_user, mock_category_map):
        """Test list_repositories filtering by Unassigned category."""
        mock_activated_repos = [
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "is_global": False},
            {"user_alias": "misc-tool", "golden_repo_alias": "misc-tool", "is_global": False},
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

            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value=mock_category_map)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute with Unassigned filter
            result = list_repositories({"category": "Unassigned"}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify - should only return repos with NULL category
            assert data["success"] is True
            assert len(data["repositories"]) == 1
            assert data["repositories"][0]["user_alias"] == "misc-tool"
            assert data["repositories"][0]["repo_category"] is None

    def test_list_repositories_sorts_by_category_priority(self, mock_user, mock_category_map):
        """Test list_repositories sorts repos by category priority, then Unassigned, then alphabetically."""
        mock_activated_repos = [
            {"user_alias": "misc-tool", "golden_repo_alias": "misc-tool", "is_global": False},
            {"user_alias": "ml-model", "golden_repo_alias": "ml-model", "is_global": False},
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "is_global": False},
            {"user_alias": "frontend-app", "golden_repo_alias": "frontend-app", "is_global": False},
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

            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.get_repo_category_map = Mock(return_value=mock_category_map)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify sorting order: Backend (1), Frontend (2), ML/AI (3), Unassigned (last)
            assert data["success"] is True
            assert len(data["repositories"]) == 4

            aliases = [r["user_alias"] for r in data["repositories"]]
            assert aliases == ["backend-api", "frontend-app", "ml-model", "misc-tool"]

    def test_list_repositories_no_category_service(self, mock_user):
        """Test list_repositories gracefully handles missing category service."""
        mock_activated_repos = [
            {"user_alias": "backend-api", "golden_repo_alias": "backend-api", "is_global": False},
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

            # No category service available
            mock_golden_manager._repo_category_service = None

            # Execute - should not fail
            result = list_repositories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify - repos should have None for category
            assert data["success"] is True
            assert len(data["repositories"]) == 1
            assert data["repositories"][0]["repo_category"] is None


class TestListRepoCategoriesHandler:
    """Test list_repo_categories handler (Step 4)."""

    def test_list_repo_categories_success(self, mock_user, mock_categories):
        """Test list_repo_categories returns all categories."""
        from code_indexer.server.mcp.handlers import list_repo_categories

        with patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager:
            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.list_categories = Mock(return_value=mock_categories)
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repo_categories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify
            assert data["success"] is True
            assert data["total"] == 3
            assert len(data["categories"]) == 3
            assert data["categories"][0]["name"] == "Backend"
            assert data["categories"][1]["name"] == "Frontend"
            assert data["categories"][2]["name"] == "ML/AI"

    def test_list_repo_categories_empty(self, mock_user):
        """Test list_repo_categories with no categories."""
        from code_indexer.server.mcp.handlers import list_repo_categories

        with patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager:
            # Mock the category service
            mock_category_service = Mock()
            mock_category_service.list_categories = Mock(return_value=[])
            mock_golden_manager._repo_category_service = mock_category_service

            # Execute
            result = list_repo_categories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify
            assert data["success"] is True
            assert data["total"] == 0
            assert data["categories"] == []

    def test_list_repo_categories_no_service(self, mock_user):
        """Test list_repo_categories handles missing service gracefully."""
        from code_indexer.server.mcp.handlers import list_repo_categories

        with patch("code_indexer.server.app.golden_repo_manager") as mock_golden_manager:
            # No category service available
            mock_golden_manager._repo_category_service = None

            # Execute
            result = list_repo_categories({}, mock_user)
            data = json.loads(result["content"][0]["text"])

            # Verify - should return error
            assert data["success"] is False
            assert "error" in data
