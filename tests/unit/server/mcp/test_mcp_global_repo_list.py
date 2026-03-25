"""
Unit tests for MCP list_repositories handler with global repo support.

Tests that list_repositories returns both activated repos AND global repos
from the golden-repos directory, with global repos properly marked.

Per Epic #520 requirement: Global repos should be visible without activation.

Bug #494: In cluster mode (storage_mode=postgres), global repos must be read
from PostgreSQL via backend_registry.global_repos, not from local SQLite/filesystem.
"""

import pytest
import os
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
def mock_global_registry_data():
    """Mock global registry data structure."""
    return {
        "cidx-meta-global": {
            "repo_name": "cidx-meta",
            "alias_name": "cidx-meta-global",
            "repo_url": None,
            "index_path": "/home/testuser/.code-indexer/golden-repos/cidx-meta",
            "created_at": "2025-11-28T08:48:12.625104+00:00",
            "last_refresh": "2025-11-28T08:48:12.625104+00:00",
        },
        "click-global": {
            "repo_name": "click",
            "alias_name": "click-global",
            "repo_url": "local:///home/testuser/.code-indexer/golden-repos/repos/click",
            "index_path": "/home/testuser/.code-indexer/golden-repos/repos/click/.code-indexer/index",
            "created_at": "2025-11-28T21:01:20.090249+00:00",
            "last_refresh": "2025-11-28T21:01:20.090249+00:00",
        },
        "pytest-global": {
            "repo_name": "pytest",
            "alias_name": "pytest-global",
            "repo_url": "local:///home/testuser/.code-indexer/golden-repos/repos/pytest",
            "index_path": "/home/testuser/.code-indexer/golden-repos/repos/pytest/.code-indexer/index",
            "created_at": "2025-11-28T21:01:27.116257+00:00",
            "last_refresh": "2025-11-28T21:01:27.116257+00:00",
        },
    }


@pytest.fixture
def mock_activated_repos():
    """Mock activated repository data."""
    return [
        {
            "user_alias": "my-project",
            "golden_repo_alias": "code-indexer",
            "branch_name": "main",
            "activated_at": "2025-11-28T10:00:00+00:00",
        },
        {
            "user_alias": "auth-lib",
            "golden_repo_alias": "test-auth-lib",
            "branch_name": "develop",
            "activated_at": "2025-11-28T11:00:00+00:00",
        },
    ]


def _setup_mock_app(mock_app, activated_repos=None, disable_access_filter=True):
    """Helper to configure standard mock_app attributes needed by list_repositories.

    Args:
        mock_app: The patched app_module mock
        activated_repos: List of activated repo dicts (default: empty list)
        disable_access_filter: If True, sets access_filtering_service to None
                               so repos are not filtered out by the access layer.
    """
    if activated_repos is None:
        activated_repos = []
    mock_app.activated_repo_manager.list_activated_repositories.return_value = (
        activated_repos
    )
    mock_category_service = Mock()
    mock_category_service.get_repo_category_map = Mock(return_value={})
    mock_app.golden_repo_manager._repo_category_service = mock_category_service
    if disable_access_filter:
        # When app_module is a MagicMock, app.state.access_filtering_service
        # is auto-created as a truthy MagicMock, which filters ALL repos out.
        # Explicitly set it to None to disable access filtering in tests.
        mock_app.app.state.access_filtering_service = None
        # Also set backend_registry to None by default (standalone mode)
        mock_app.app.state.backend_registry = None


class TestListRepositoriesWithGlobalRepos:
    """Test MCP list_repositories handler includes global repos."""

    def test_global_repos_appear_in_list(
        self, mock_user, mock_global_registry_data, mock_activated_repos
    ):
        """Test that global repos from registry appear in list_repositories response."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=mock_activated_repos)

            # Mock GlobalRegistry to return global repos
            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                assert "content" in result
                assert len(result["content"]) == 1
                assert result["content"][0]["type"] == "text"

                import json

                response_data = json.loads(result["content"][0]["text"])

                assert response_data["success"] is True
                assert "repositories" in response_data

                repos = response_data["repositories"]
                assert len(repos) == 5

                global_repo_aliases = [
                    repo["user_alias"]
                    for repo in repos
                    if repo.get("is_global") is True
                ]
                assert "cidx-meta-global" in global_repo_aliases
                assert "click-global" in global_repo_aliases
                assert "pytest-global" in global_repo_aliases

                activated_aliases = [
                    repo["user_alias"]
                    for repo in repos
                    if repo.get("is_global") is not True
                ]
                assert "my-project" in activated_aliases
                assert "auth-lib" in activated_aliases

    def test_global_repos_marked_with_is_global_true(
        self, mock_user, mock_global_registry_data, mock_activated_repos
    ):
        """Test that global repos have is_global: true field."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=mock_activated_repos)

            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])
                repos = response_data["repositories"]

                for repo in repos:
                    if repo.get("user_alias", "").endswith("-global"):
                        assert (
                            repo["is_global"] is True
                        ), f"Global repo {repo['user_alias']} missing is_global=True"

                for repo in repos:
                    if not repo.get("user_alias", "").endswith("-global"):
                        assert (
                            repo.get("is_global") is not True
                        ), "Activated repo should not have is_global=True"

    def test_global_repos_include_metadata(self, mock_user, mock_global_registry_data):
        """Test that global repos include repo name, last update time."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=[])

            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])
                repos = response_data["repositories"]

                for repo in repos:
                    if repo.get("is_global") is True:
                        assert (
                            "golden_repo_alias" in repo
                        ), "Global repo missing golden_repo_alias"
                        assert "user_alias" in repo, "Global repo missing user_alias"
                        assert (
                            "last_refresh" in repo
                        ), "Global repo missing last_refresh"
                        assert (
                            "index_path" not in repo
                        ), "Global repo should not have index_path"
                        assert repo["user_alias"].endswith("-global")

    def test_empty_global_registry_handled_gracefully(
        self, mock_user, mock_activated_repos
    ):
        """Test that empty golden-repos directory is handled without errors."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=mock_activated_repos)

            mock_repos_list = []

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])

                assert response_data["success"] is True

                repos = response_data["repositories"]
                assert len(repos) == 2
                assert all(
                    repo.get("is_global") is not True for repo in repos
                ), "Should not have any global repos"

    def test_only_global_repos_no_activated_repos(
        self, mock_user, mock_global_registry_data
    ):
        """Test list_repositories when user has no activated repos but global repos exist."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=[])

            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])
                repos = response_data["repositories"]

                assert len(repos) == 3
                assert all(
                    repo["is_global"] is True for repo in repos
                ), "All repos should be global"

    def test_global_registry_error_does_not_break_activated_list(self, mock_user):
        """Test that global registry errors don't prevent listing activated repos."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(
                mock_app,
                activated_repos=[
                    {
                        "user_alias": "my-project",
                        "golden_repo_alias": "code-indexer",
                        "branch_name": "main",
                    }
                ],
            )

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                side_effect=Exception("Registry load failed"),
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])

                assert response_data["success"] is True
                repos = response_data["repositories"]

                assert len(repos) >= 1
                assert any(
                    repo.get("user_alias") == "my-project" for repo in repos
                ), "Activated repo should still be listed despite global registry error"

    def test_golden_repos_dir_from_environment(self, mock_user, tmp_path):
        """Test that golden_repos_dir is loaded from app.state."""
        temp_golden_dir = tmp_path / "test-golden-repos"
        temp_golden_dir.mkdir(parents=True)

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch.dict(os.environ, {"GOLDEN_REPOS_DIR": str(temp_golden_dir)}),
        ):
            mock_app.app.state.golden_repos_dir = str(temp_golden_dir)
            mock_app.app.state.access_filtering_service = None
            mock_app.app.state.backend_registry = None  # Standalone mode

            _setup_mock_app(mock_app, activated_repos=[], disable_access_filter=False)
            mock_app.app.state.access_filtering_service = None

            mock_repos_list = []

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ) as mock_registry_factory:
                list_repositories({}, mock_user)

                mock_registry_factory.assert_called_once()

    def test_duplicate_alias_names_handled(self, mock_user, mock_global_registry_data):
        """Test that duplicate alias names between activated and global repos are handled."""
        duplicate_activated = [
            {
                "user_alias": "click-global",
                "golden_repo_alias": "click",
                "branch_name": "main",
            }
        ]

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=duplicate_activated)

            mock_repos_list = [mock_global_registry_data["click-global"]]

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ):
                result = list_repositories({}, mock_user)

                import json

                response_data = json.loads(result["content"][0]["text"])
                repos = response_data["repositories"]

                assert len(repos) == 2

                global_count = sum(1 for repo in repos if repo.get("is_global") is True)
                activated_count = sum(
                    1 for repo in repos if repo.get("is_global") is not True
                )

                assert global_count == 1, "Should have 1 global repo"
                assert activated_count == 1, "Should have 1 activated repo"


class TestListRepositoriesClusterMode:
    """Test MCP list_repositories handler in cluster mode (Bug #494).

    In cluster mode (storage_mode=postgres), global repos MUST be read from
    PostgreSQL via backend_registry.global_repos, not from local SQLite files.
    """

    def test_unified_backend_returns_all_global_repos(
        self, mock_user, mock_global_registry_data
    ):
        """Bug #494/#495: list_repositories always uses _list_global_repos().

        ONE code path for both SQLite and PostgreSQL modes via BackendRegistry.
        No cluster vs standalone branching.
        """
        import json

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=[])

            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ) as mock_list_fn:
                result = list_repositories({}, mock_user)

                mock_list_fn.assert_called_once()

                response_data = json.loads(result["content"][0]["text"])
                assert response_data["success"] is True
                repos = response_data["repositories"]

                assert len(repos) == 3
                assert all(r["is_global"] is True for r in repos)

                global_aliases = {r["user_alias"] for r in repos}
                assert "cidx-meta-global" in global_aliases
                assert "click-global" in global_aliases
                assert "pytest-global" in global_aliases

    def test_standalone_mode_uses_sqlite_registry(
        self, mock_user, mock_global_registry_data
    ):
        """In standalone mode (no backend_registry), SQLite GlobalRegistry is used."""
        import json

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=[])
            mock_app.app.state.backend_registry = None
            mock_app.app.state.golden_repos_dir = "/fake/golden-repos"

            mock_repos_list = list(mock_global_registry_data.values())

            with patch(
                "code_indexer.server.mcp.handlers._list_global_repos",
                return_value=mock_repos_list,
            ) as mock_registry_factory:
                result = list_repositories({}, mock_user)

                response_data = json.loads(result["content"][0]["text"])
                assert response_data["success"] is True
                repos = response_data["repositories"]

                assert len(repos) == 3
                mock_registry_factory.assert_called_once()

    def test_cluster_mode_backend_registry_error_falls_back_gracefully(self, mock_user):
        """If backend_registry.global_repos.list_repos() fails, handler returns activated repos only."""
        import json

        mock_backend_registry = Mock()
        mock_global_repos_backend = Mock()
        mock_global_repos_backend.list_repos.side_effect = Exception(
            "PostgreSQL connection failed"
        )
        mock_backend_registry.global_repos = mock_global_repos_backend

        activated = [
            {
                "user_alias": "my-project",
                "golden_repo_alias": "code-indexer",
                "current_branch": "main",
                "is_global": False,
                "repo_url": None,
                "last_refresh": None,
            }
        ]

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=activated)
            mock_app.app.state.backend_registry = mock_backend_registry
            mock_app.app.state.golden_repos_dir = "/fake/golden-repos"

            result = list_repositories({}, mock_user)

            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            repos = response_data["repositories"]

            assert len(repos) == 1
            assert repos[0]["user_alias"] == "my-project"

    def test_cluster_mode_with_no_global_repos_returns_empty_global_list(
        self, mock_user
    ):
        """In cluster mode, if PostgreSQL has no global repos, result has no global repos."""
        import json

        mock_backend_registry = Mock()
        mock_global_repos_backend = Mock()
        mock_global_repos_backend.list_repos.return_value = {}
        mock_backend_registry.global_repos = mock_global_repos_backend

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            _setup_mock_app(mock_app, activated_repos=[])
            mock_app.app.state.backend_registry = mock_backend_registry
            mock_app.app.state.golden_repos_dir = "/fake/golden-repos"

            result = list_repositories({}, mock_user)

            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            repos = response_data["repositories"]
            assert repos == []
