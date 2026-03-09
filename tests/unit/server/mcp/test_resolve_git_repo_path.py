"""
Unit tests for _resolve_git_repo_path user-activated repo .git validation (P1-1 fix).

Verifies that user-activated repos without a .git directory return a clear error
message instead of allowing cryptic git command failures downstream.

Tests:
1. User-activated repo with .git directory returns path successfully
2. User-activated repo WITHOUT .git directory returns error message
3. User-activated repo that doesn't exist (path is None) returns error message
"""

from unittest.mock import MagicMock, patch
import pytest


class TestResolveGitRepoPathUserActivated:
    """Tests for user-activated repo path in _resolve_git_repo_path."""

    def test_user_activated_repo_with_git_dir_returns_path(self, tmp_path):
        """User-activated repo with .git directory returns path and no error."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        with patch(
            "code_indexer.server.mcp.handlers.ActivatedRepoManager"
        ) as MockClass:
            mock_instance = MockClass.return_value
            mock_instance.get_activated_repo_path.return_value = str(repo_dir)

            path, error_msg = _resolve_git_repo_path("my-repo", "testuser")

        assert error_msg is None
        assert path == str(repo_dir)

    def test_user_activated_repo_without_git_dir_returns_error(self, tmp_path):
        """User-activated repo without .git directory returns error message."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "local-repo"
        repo_dir.mkdir()
        # No .git directory created

        with patch(
            "code_indexer.server.mcp.handlers.ActivatedRepoManager"
        ) as MockClass:
            mock_instance = MockClass.return_value
            mock_instance.get_activated_repo_path.return_value = str(repo_dir)

            path, error_msg = _resolve_git_repo_path("local-repo", "testuser")

        assert path is None
        assert error_msg is not None
        assert "local-repo" in error_msg
        assert "does not support git operations" in error_msg

    def test_user_activated_repo_none_path_returns_error(self):
        """User-activated repo returning None path returns error message."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        with patch(
            "code_indexer.server.mcp.handlers.ActivatedRepoManager"
        ) as MockClass:
            mock_instance = MockClass.return_value
            mock_instance.get_activated_repo_path.return_value = None

            path, error_msg = _resolve_git_repo_path("missing-repo", "testuser")

        assert path is None
        assert error_msg is not None
        assert "missing-repo" in error_msg
        assert "not found" in error_msg


class TestResolveGitRepoPathGroupAccess:
    """Story #387: Group access check in _resolve_git_repo_path for global repos.

    Tests that inaccessible global repos return "not found" (invisible repo
    pattern - same error as non-existent repo), and accessible repos pass through.
    """

    def test_inaccessible_global_repo_returns_not_found(self, tmp_path):
        """Inaccessible global repo returns 'not found' (invisible repo pattern)."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "my-repo" / "v_12345"
        repo_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        # Access filtering service returns set WITHOUT this repo
        mock_afs = MagicMock()
        mock_afs.get_accessible_repos.return_value = {"other-repo", "cidx-meta"}

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value=str(repo_dir),
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.get_server_global_registry"
                ) as mock_registry_fn:
                    mock_registry = MagicMock()
                    mock_registry.get_global_repo.return_value = {
                        "repo_url": "git@github.com:org/my-repo.git"
                    }
                    mock_registry_fn.return_value = mock_registry

                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=mock_afs,
                    ):
                        path, error_msg = _resolve_git_repo_path("my-repo-global", "testuser")

        assert path is None
        assert error_msg is not None
        assert "not found" in error_msg
        # Must NOT reveal that repo exists but is access-denied (invisible repo pattern)
        assert "access" not in error_msg.lower()
        assert "permission" not in error_msg.lower()

    def test_accessible_global_repo_passes_through(self, tmp_path):
        """Global repo accessible by user group passes through and returns path."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "my-repo" / "v_12345"
        repo_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        # Access filtering service returns set WITH this repo (without -global suffix)
        mock_afs = MagicMock()
        mock_afs.get_accessible_repos.return_value = {"my-repo", "cidx-meta"}

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value=str(repo_dir),
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.get_server_global_registry"
                ) as mock_registry_fn:
                    mock_registry = MagicMock()
                    mock_registry.get_global_repo.return_value = {
                        "repo_url": "git@github.com:org/my-repo.git"
                    }
                    mock_registry_fn.return_value = mock_registry

                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=mock_afs,
                    ):
                        path, error_msg = _resolve_git_repo_path("my-repo-global", "testuser")

        assert error_msg is None
        assert path == str(repo_dir)

    def test_no_access_filtering_service_global_repo_passes_through(self, tmp_path):
        """When no access filtering service configured, global repos pass through (backward compat)."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "my-repo" / "v_12345"
        repo_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value=str(repo_dir),
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.get_server_global_registry"
                ) as mock_registry_fn:
                    mock_registry = MagicMock()
                    mock_registry.get_global_repo.return_value = {
                        "repo_url": "git@github.com:org/my-repo.git"
                    }
                    mock_registry_fn.return_value = mock_registry

                    # No access filtering service (returns None)
                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=None,
                    ):
                        path, error_msg = _resolve_git_repo_path("my-repo-global", "testuser")

        assert error_msg is None
        assert path == str(repo_dir)

    def test_access_check_uses_alias_without_global_suffix(self, tmp_path):
        """Access check strips -global suffix when comparing to accessible repos set."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        repo_dir = tmp_path / "code-indexer-python" / "v_12345"
        repo_dir.mkdir(parents=True)
        git_dir = repo_dir / ".git"
        git_dir.mkdir()

        # Accessible set stores base name without -global
        mock_afs = MagicMock()
        mock_afs.get_accessible_repos.return_value = {"code-indexer-python", "cidx-meta"}

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ):
            with patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path",
                return_value=str(repo_dir),
            ):
                with patch(
                    "code_indexer.server.mcp.handlers.get_server_global_registry"
                ) as mock_registry_fn:
                    mock_registry = MagicMock()
                    mock_registry.get_global_repo.return_value = {
                        "repo_url": "git@github.com:org/code-indexer.git"
                    }
                    mock_registry_fn.return_value = mock_registry

                    with patch(
                        "code_indexer.server.mcp.handlers._get_access_filtering_service",
                        return_value=mock_afs,
                    ):
                        # Alias has -global suffix; access set has base name
                        path, error_msg = _resolve_git_repo_path(
                            "code-indexer-python-global", "testuser"
                        )

        assert error_msg is None
        assert path == str(repo_dir)
