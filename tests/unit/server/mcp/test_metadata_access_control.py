"""
Tests for group-based access control in metadata listing MCP handlers (Story #316).

Verifies that handle_list_global_repos and get_all_repositories_status apply
group-based access filtering before returning results.

AC1: handle_list_global_repos filters repos by user group membership
AC1-compat: handle_list_global_repos with no service returns all (backward compat)
AC2: handle_list_global_repos admin sees all repos (no filtering applied)
AC3: get_all_repositories_status filters global repos by user group membership
AC3-compat: get_all_repositories_status with no service returns all (backward compat)
AC4: get_all_repositories_status admin sees all global repos
"""

import json
import pytest
from unittest.mock import Mock, patch

from code_indexer.server.mcp.handlers import (
    handle_list_global_repos,
    get_all_repositories_status,
)
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_regular_user():
    """Create a mock regular user for testing."""
    user = Mock(spec=User)
    user.username = "regularuser"
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
def mock_access_filtering_service():
    """Create a mock AccessFilteringService."""
    service = Mock()
    # Default: filter_repo_listing returns only allowed repos
    service.filter_repo_listing = Mock(side_effect=lambda repos, user_id: repos)
    # Default: is_admin_user returns False
    service.is_admin_user = Mock(return_value=False)
    return service


# ---------------------------------------------------------------------------
# AC1: handle_list_global_repos filters repos by group membership
# ---------------------------------------------------------------------------


class TestListGlobalReposFiltersByGroup:
    """AC1: handle_list_global_repos applies group-based access filtering."""

    def test_list_global_repos_filters_by_group(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC1: After getting repos from GlobalRepoOperations.list_repos(),
        apply filter_repo_listing(repo_aliases, user.username).

        Regular user should only see repos their group has access to.
        filter_repo_listing MUST be called with the list of repo names and username.
        The blocked repo must NOT appear in the response.
        """
        # ops.list_repos() returns dicts with "alias" and "repo_name" keys
        all_repos = [
            {
                "alias": "allowed-repo-global",
                "repo_name": "allowed-repo",
                "url": "https://example.com/allowed",
                "last_refresh": None,
            },
            {
                "alias": "blocked-repo-global",
                "repo_name": "blocked-repo",
                "url": "https://example.com/blocked",
                "last_refresh": None,
            },
        ]

        # Filter returns only "allowed-repo" (by repo_name)
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["allowed-repo"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.shared_operations.GlobalRepoOperations"
            ) as mock_ops_class,
        ):
            mock_ops_instance = Mock()
            mock_ops_instance.list_repos.return_value = all_repos
            mock_ops_class.return_value = mock_ops_instance
            mock_app_module.app.state = mock_app_state

            result = handle_list_global_repos({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing MUST have been called with username
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "regularuser"

        # Only the allowed repo should be in the response
        repo_names = [r["repo_name"] for r in data["repos"]]
        assert "allowed-repo" in repo_names
        assert "blocked-repo" not in repo_names

    def test_list_global_repos_no_service_returns_all(
        self, mock_regular_user
    ):
        """
        AC1-compat: When no AccessFilteringService is configured
        (access_filtering_service=None), all repos are returned without
        filtering (backward-compatible behavior).
        """
        all_repos = [
            {
                "alias": "repo-a-global",
                "repo_name": "repo-a",
                "url": "https://example.com/a",
                "last_refresh": None,
            },
            {
                "alias": "repo-b-global",
                "repo_name": "repo-b",
                "url": "https://example.com/b",
                "last_refresh": None,
            },
        ]

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = None

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.shared_operations.GlobalRepoOperations"
            ) as mock_ops_class,
        ):
            mock_ops_instance = Mock()
            mock_ops_instance.list_repos.return_value = all_repos
            mock_ops_class.return_value = mock_ops_instance
            mock_app_module.app.state = mock_app_state

            result = handle_list_global_repos({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert len(data["repos"]) == 2


# ---------------------------------------------------------------------------
# AC2: handle_list_global_repos admin sees all repos
# ---------------------------------------------------------------------------


class TestListGlobalReposAdminSeesAll:
    """AC2: Admin user sees all repos (filter_repo_listing called with admin username)."""

    def test_list_global_repos_admin_sees_all(
        self, mock_admin_user, mock_access_filtering_service
    ):
        """
        AC2: When admin user calls handle_list_global_repos, filter_repo_listing
        is called with admin username. The service itself handles admin bypass
        by returning ALL repo names.
        """
        all_repos = [
            {
                "alias": "repo-a-global",
                "repo_name": "repo-a",
                "url": "https://example.com/a",
                "last_refresh": None,
            },
            {
                "alias": "repo-b-global",
                "repo_name": "repo-b",
                "url": "https://example.com/b",
                "last_refresh": None,
            },
        ]

        # Admin: filter returns ALL repo names (service handles admin bypass)
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["repo-a", "repo-b"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.global_repos.shared_operations.GlobalRepoOperations"
            ) as mock_ops_class,
        ):
            mock_ops_instance = Mock()
            mock_ops_instance.list_repos.return_value = all_repos
            mock_ops_class.return_value = mock_ops_instance
            mock_app_module.app.state = mock_app_state

            result = handle_list_global_repos({}, mock_admin_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing called with admin username
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "admin"

        # Admin sees both repos
        repo_names = [r["repo_name"] for r in data["repos"]]
        assert "repo-a" in repo_names
        assert "repo-b" in repo_names


# ---------------------------------------------------------------------------
# AC3: get_all_repositories_status filters global repos by group membership
# ---------------------------------------------------------------------------


class TestGetAllRepositoriesStatusFiltersGlobalRepos:
    """AC3: get_all_repositories_status filters global repos by user group membership."""

    def test_get_all_repositories_status_filters_global_repos(
        self, mock_regular_user, mock_access_filtering_service
    ):
        """
        AC3: After getting global repos from registry.list_global_repos(),
        apply filter_repo_listing(repo_names, user.username).

        Regular user should only see global repos their group has access to.
        The blocked global repo must NOT appear in the repositories list.
        """
        # registry.list_global_repos() returns dicts with "repo_name", "alias_name" keys
        global_repos_data = [
            {
                "alias_name": "allowed-repo-global",
                "repo_name": "allowed-repo",
                "repo_url": "https://example.com/allowed",
                "last_refresh": None,
                "index_path": "/mock/path/allowed",
                "created_at": None,
            },
            {
                "alias_name": "blocked-repo-global",
                "repo_name": "blocked-repo",
                "repo_url": "https://example.com/blocked",
                "last_refresh": None,
                "index_path": "/mock/path/blocked",
                "created_at": None,
            },
        ]

        # Filter returns only "allowed-repo"
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["allowed-repo"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
        ):
            mock_app_module.app.state = mock_app_state
            # No activated repos for simplicity
            mock_app_module.activated_repo_manager.list_activated_repositories = Mock(
                return_value=[]
            )

            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=global_repos_data)
            mock_registry_factory.return_value = mock_registry

            result = get_all_repositories_status({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing MUST have been called with username
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "regularuser"

        # Only the allowed global repo should appear
        golden_aliases = [r.get("golden_repo_alias") for r in data["repositories"]]
        assert "allowed-repo" in golden_aliases
        assert "blocked-repo" not in golden_aliases

    def test_get_all_repositories_status_no_service_returns_all(
        self, mock_regular_user
    ):
        """
        AC3-compat: When no AccessFilteringService configured, all global repos
        are returned without filtering (backward-compatible behavior).
        """
        global_repos_data = [
            {
                "alias_name": "repo-a-global",
                "repo_name": "repo-a",
                "repo_url": None,
                "last_refresh": None,
                "index_path": None,
                "created_at": None,
            },
            {
                "alias_name": "repo-b-global",
                "repo_name": "repo-b",
                "repo_url": None,
                "last_refresh": None,
                "index_path": None,
                "created_at": None,
            },
        ]

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = None

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.activated_repo_manager.list_activated_repositories = Mock(
                return_value=[]
            )

            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=global_repos_data)
            mock_registry_factory.return_value = mock_registry

            result = get_all_repositories_status({}, mock_regular_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        # Both repos should appear (no filtering)
        assert data["total"] == 2


# ---------------------------------------------------------------------------
# AC4: get_all_repositories_status admin sees all global repos
# ---------------------------------------------------------------------------


class TestGetAllRepositoriesStatusAdminSeesAll:
    """AC4: Admin user sees all global repos in get_all_repositories_status."""

    def test_get_all_repositories_status_admin_sees_all(
        self, mock_admin_user, mock_access_filtering_service
    ):
        """
        AC4: When admin user calls get_all_repositories_status,
        filter_repo_listing is called with admin username.
        The service returns all repo names (admin bypass).
        """
        global_repos_data = [
            {
                "alias_name": "repo-a-global",
                "repo_name": "repo-a",
                "repo_url": None,
                "last_refresh": None,
                "index_path": None,
                "created_at": None,
            },
            {
                "alias_name": "repo-b-global",
                "repo_name": "repo-b",
                "repo_url": None,
                "last_refresh": None,
                "index_path": None,
                "created_at": None,
            },
        ]

        # Admin: filter returns ALL repo names
        mock_access_filtering_service.filter_repo_listing = Mock(
            return_value=["repo-a", "repo-b"]
        )

        mock_app_state = Mock()
        mock_app_state.access_filtering_service = mock_access_filtering_service

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/mock/golden-repos",
            ),
            patch(
                "code_indexer.server.mcp.handlers.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory,
        ):
            mock_app_module.app.state = mock_app_state
            mock_app_module.activated_repo_manager.list_activated_repositories = Mock(
                return_value=[]
            )

            mock_registry = Mock()
            mock_registry.list_global_repos = Mock(return_value=global_repos_data)
            mock_registry_factory.return_value = mock_registry

            result = get_all_repositories_status({}, mock_admin_user)

        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True

        # filter_repo_listing called with admin username
        mock_access_filtering_service.filter_repo_listing.assert_called_once()
        call_args = mock_access_filtering_service.filter_repo_listing.call_args
        assert call_args[0][1] == "admin"

        # Admin sees both global repos
        golden_aliases = [r.get("golden_repo_alias") for r in data["repositories"]]
        assert "repo-a" in golden_aliases
        assert "repo-b" in golden_aliases
        assert data["total"] == 2
