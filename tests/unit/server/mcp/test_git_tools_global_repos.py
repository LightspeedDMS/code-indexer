"""
Unit tests for _resolve_git_repo_path helper (BUG-2 fix).

Verifies that git operations against global repos return clear error messages
instead of filesystem errors when the repo has no .git directory (e.g. local://
repos like cidx-meta-global).

Tests:
1. _resolve_git_repo_path returns error for global repos without .git
2. _resolve_git_repo_path returns correct path for global repos with .git
3. _resolve_git_repo_path returns activated-repo path for non-global aliases
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create a test user."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
        email="testuser@example.com",
    )


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    content = mcp_response["content"][0]
    return cast(dict, json.loads(content["text"]))


def _make_mock_registry(repo_url: str = "local://cidx-meta"):
    """Return a mock GlobalRegistry whose get_global_repo returns a local:// entry."""
    mock_registry = MagicMock()
    mock_registry.get_global_repo.return_value = {"repo_url": repo_url}
    mock_get_registry = MagicMock(return_value=mock_registry)
    return mock_get_registry


def _make_mock_registry_not_found():
    """Return a mock GlobalRegistry whose get_global_repo returns None (not found)."""
    mock_registry = MagicMock()
    mock_registry.get_global_repo.return_value = None
    mock_get_registry = MagicMock(return_value=mock_registry)
    return mock_get_registry


def _make_mock_registry_git_repo(repo_url: str = "https://github.com/org/my-repo.git"):
    """Return a mock GlobalRegistry whose get_global_repo returns a git:// entry."""
    mock_registry = MagicMock()
    mock_registry.get_global_repo.return_value = {"repo_url": repo_url}
    mock_get_registry = MagicMock(return_value=mock_registry)
    return mock_get_registry


class TestResolveGitRepoPath:
    """Tests for _resolve_git_repo_path helper function."""

    def test_returns_error_for_local_global_repo_no_git_dir(self, tmp_path):
        """Global repo without .git directory returns error message (local:// repos)."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        # A directory exists but has no .git subdirectory
        repo_dir = tmp_path / "cidx-meta"
        repo_dir.mkdir()

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=str(repo_dir),
        ):
            path, error_msg = _resolve_git_repo_path("cidx-meta-global", "testuser")

        assert path is None
        assert error_msg is not None
        assert "cidx-meta-global" in error_msg
        assert "local repository" in error_msg
        assert "does not support git operations" in error_msg

    def test_returns_error_when_global_repo_not_found(self, tmp_path):
        """Global repo that cannot be resolved returns error message."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry_not_found(),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=None,
        ):
            path, error_msg = _resolve_git_repo_path("missing-global", "testuser")

        assert path is None
        assert error_msg is not None
        assert "missing-global" in error_msg
        assert "not found" in error_msg

    def test_returns_path_for_global_repo_with_git_dir(self, tmp_path):
        """Global repo with .git directory returns resolved path and no error."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        # Create a proper git repo directory structure
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=str(tmp_path),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry_git_repo(),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=str(repo_dir),
        ):
            path, error_msg = _resolve_git_repo_path("my-repo-global", "testuser")

        assert error_msg is None
        assert path == str(repo_dir)

    def test_returns_activated_repo_path_for_non_global_alias(self, tmp_path):
        """Non-global alias returns path from ActivatedRepoManager with no error."""
        from code_indexer.server.mcp.handlers import _resolve_git_repo_path

        # Create a real directory with .git so the validation passes
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
        mock_instance.get_activated_repo_path.assert_called_once_with(
            username="testuser", user_alias="my-repo"
        )


class TestGitHandlersWithGlobalRepo:
    """Tests verifying second-gen handlers return graceful errors for global repos."""

    @pytest.fixture
    def mock_local_global_repo(self, tmp_path):
        """Mock a global repo that is local:// (no .git dir)."""
        repo_dir = tmp_path / "cidx-meta"
        repo_dir.mkdir()
        return str(repo_dir)

    def test_git_status_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """git_status returns clear error message for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.git_status(
                {"repository_alias": "cidx-meta-global"}, mock_user
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_git_log_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """git_log returns clear error message for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.git_log(
                {"repository_alias": "cidx-meta-global"}, mock_user
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_git_diff_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """git_diff returns clear error message for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.git_diff(
                {"repository_alias": "cidx-meta-global"}, mock_user
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]


class TestFirstGenGitHandlersWithGlobalRepo:
    """Tests verifying first-gen handlers return graceful errors for global repos."""

    @pytest.fixture
    def mock_local_global_repo(self, tmp_path):
        """Mock a global repo that is local:// (no .git dir)."""
        repo_dir = tmp_path / "cidx-meta"
        repo_dir.mkdir()
        return str(repo_dir)

    def test_handle_git_show_commit_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_show_commit returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_show_commit(
                {"repository_alias": "cidx-meta-global", "commit_hash": "abc123"},
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_handle_git_file_at_revision_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_file_at_revision returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_file_at_revision(
                {
                    "repository_alias": "cidx-meta-global",
                    "path": "README.md",
                    "revision": "HEAD",
                },
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_handle_git_blame_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_blame returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_blame(
                {"repository_alias": "cidx-meta-global", "path": "README.md"},
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_handle_git_file_history_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_file_history returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_file_history(
                {"repository_alias": "cidx-meta-global", "path": "README.md"},
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_handle_git_search_commits_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_search_commits returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_search_commits(
                {"repository_alias": "cidx-meta-global", "query": "fix bug"},
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]

    def test_handle_git_search_diffs_returns_error_for_local_global_repo(
        self, mock_user, mock_local_global_repo
    ):
        """handle_git_search_diffs returns clear error for local:// global repos."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir",
            return_value=os.path.dirname(mock_local_global_repo),
        ), patch(
            "code_indexer.server.mcp.handlers.get_server_global_registry",
            _make_mock_registry("local://cidx-meta"),
        ), patch(
            "code_indexer.server.mcp.handlers._resolve_repo_path",
            return_value=mock_local_global_repo,
        ):
            response = handlers.handle_git_search_diffs(
                {
                    "repository_alias": "cidx-meta-global",
                    "search_string": "fix bug",
                },
                mock_user,
            )

        data = _extract_response_data(response)
        assert data["success"] is False
        assert "cidx-meta-global" in data["error"]
        assert "does not support git operations" in data["error"]
