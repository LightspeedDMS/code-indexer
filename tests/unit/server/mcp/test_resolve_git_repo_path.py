"""
Unit tests for _resolve_git_repo_path user-activated repo .git validation (P1-1 fix).

Verifies that user-activated repos without a .git directory return a clear error
message instead of allowing cryptic git command failures downstream.

Tests:
1. User-activated repo with .git directory returns path successfully
2. User-activated repo WITHOUT .git directory returns error message
3. User-activated repo that doesn't exist (path is None) returns error message
"""

from unittest.mock import patch
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
