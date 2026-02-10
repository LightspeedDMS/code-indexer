"""
Unit tests for get_branches handler with non-git repositories (Story #163 AC3).

Tests that get_branches MCP handler checks is_git_available() before calling
BranchService and returns empty branches array for non-git repos.
"""

import pytest
import tempfile
import shutil
import json
from pathlib import Path
from unittest.mock import Mock, MagicMock
from git import Repo

from code_indexer.server.mcp.handlers import get_branches


class TestGetBranchesHandlerNonGit:
    """Test cases for get_branches handler with non-git repositories (AC3)."""

    def setup_method(self):
        """Set up test environment."""
        self.temp_dirs = []

    def teardown_method(self):
        """Clean up test directories."""
        for temp_dir in self.temp_dirs:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    def _create_temp_dir(self) -> Path:
        """Create a temporary directory and track it for cleanup."""
        temp_dir = Path(tempfile.mkdtemp())
        self.temp_dirs.append(temp_dir)
        return temp_dir

    def test_get_branches_returns_empty_for_non_git_repository(self):
        """
        AC3: Given a non-git folder registered as a repository,
        when get_branches MCP handler is called,
        then it should check is_git_available() before calling BranchService
        and it should return empty branches array for non-git repos.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        # Mock user
        user = Mock()
        user.username = "testuser"

        # Mock app_module with activated_repo_manager (must be set as module attribute for handler)
        import code_indexer.server.mcp.handlers as handlers_module

        original_app_module = getattr(handlers_module, "app_module", None)
        try:
            mock_app_module = Mock()
            mock_app_module.activated_repo_manager = Mock()
            mock_app_module.activated_repo_manager.get_activated_repo_path.return_value = str(
                non_git_dir
            )
            handlers_module.app_module = mock_app_module

            # Act
            params = {"repository_alias": "test-repo", "include_remote": False}
            result = get_branches(params=params, user=user)

            # Assert - Parse MCP response format
            assert "content" in result
            assert len(result["content"]) > 0
            assert result["content"][0]["type"] == "text"

            # Parse JSON from response
            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            assert "branches" in response_data
            assert response_data["branches"] == []
            assert len(response_data["branches"]) == 0
        finally:
            # Restore original app_module
            if original_app_module is not None:
                handlers_module.app_module = original_app_module
            elif hasattr(handlers_module, "app_module"):
                delattr(handlers_module, "app_module")

    def test_get_branches_works_normally_for_git_repository(self):
        """
        AC3 Extension: Verify git repositories still work correctly.
        """
        # Arrange - Create git repository
        git_dir = self._create_temp_dir()
        repo = Repo.init(git_dir)
        repo.config_writer().set_value("user", "name", "Test User").release()
        repo.config_writer().set_value("user", "email", "test@example.com").release()

        # Create initial commit
        test_file = git_dir / "test.py"
        test_file.write_text("print('hello')")
        repo.index.add([str(test_file)])
        repo.index.commit("Initial commit")

        # Create additional branch
        repo.create_head("develop")

        # Mock user
        user = Mock()
        user.username = "testuser"

        # Mock app_module
        import code_indexer.server.mcp.handlers as handlers_module

        original_app_module = getattr(handlers_module, "app_module", None)
        try:
            mock_app_module = Mock()
            mock_app_module.activated_repo_manager = Mock()
            mock_app_module.activated_repo_manager.get_activated_repo_path.return_value = str(
                git_dir
            )
            handlers_module.app_module = mock_app_module

            # Act
            params = {"repository_alias": "test-repo", "include_remote": False}
            result = get_branches(params=params, user=user)

            # Assert - Parse MCP response format
            assert "content" in result
            assert len(result["content"]) > 0
            assert result["content"][0]["type"] == "text"

            # Parse JSON from response
            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            assert "branches" in response_data
            assert len(response_data["branches"]) == 2  # master and develop
            branch_names = {branch["name"] for branch in response_data["branches"]}
            assert "master" in branch_names or "main" in branch_names
            assert "develop" in branch_names
        finally:
            if original_app_module is not None:
                handlers_module.app_module = original_app_module
            elif hasattr(handlers_module, "app_module"):
                delattr(handlers_module, "app_module")

    def test_get_branches_with_include_remote_for_non_git(self):
        """
        AC3 Extension: include_remote parameter should also work for non-git.
        """
        # Arrange - Create non-git directory
        non_git_dir = self._create_temp_dir()
        test_file = non_git_dir / "test.txt"
        test_file.write_text("test content")

        # Mock user
        user = Mock()
        user.username = "testuser"

        # Mock app_module
        import code_indexer.server.mcp.handlers as handlers_module

        original_app_module = getattr(handlers_module, "app_module", None)
        try:
            mock_app_module = Mock()
            mock_app_module.activated_repo_manager = Mock()
            mock_app_module.activated_repo_manager.get_activated_repo_path.return_value = str(
                non_git_dir
            )
            handlers_module.app_module = mock_app_module

            # Act
            params = {
                "repository_alias": "test-repo",
                "include_remote": True,  # This should not cause issues for non-git
            }
            result = get_branches(params=params, user=user)

            # Assert - Parse MCP response format
            assert "content" in result
            assert len(result["content"]) > 0
            assert result["content"][0]["type"] == "text"

            # Parse JSON from response
            response_data = json.loads(result["content"][0]["text"])
            assert response_data["success"] is True
            assert "branches" in response_data
            assert response_data["branches"] == []
        finally:
            if original_app_module is not None:
                handlers_module.app_module = original_app_module
            elif hasattr(handlers_module, "app_module"):
                delattr(handlers_module, "app_module")
