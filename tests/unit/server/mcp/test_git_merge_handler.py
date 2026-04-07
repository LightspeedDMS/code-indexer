"""
Unit tests for git_merge MCP handler (Story #388).

Tests handler parameter validation, write-mode enforcement,
group access control, clean merge, and conflict merge responses.

All tests mock at the service/helper function level following existing patterns.
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.git_operations_service import GitCommandError
from code_indexer.server.mcp import handlers


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_user():
    """Create a regular user for testing."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
        email="testuser@example.com",
    )


@pytest.fixture
def mock_git_service():
    """Patch git_operations_service in handlers module."""
    with patch(
        "code_indexer.server.mcp.handlers.git_operations_service"
    ) as mock_service:
        yield mock_service


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual data from MCP response wrapper."""
    content = mcp_response["content"][0]
    return cast(dict, json.loads(content["text"]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGitMergeHandlerParameterValidation:
    """Test parameter validation in git_merge handler."""

    def test_handler_requires_repository_alias(self, mock_user):
        """Missing repository_alias returns error."""
        params = {"source_branch": "feature"}

        mcp_response = handlers.git_merge(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_requires_source_branch(self, mock_user):
        """Missing source_branch returns error."""
        params = {"repository_alias": "test-repo"}

        mcp_response = handlers.git_merge(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "source_branch" in data["error"]


class TestGitMergeHandlerWriteMode:
    """Test write-mode enforcement in git_merge handler."""

    def test_handler_enforces_write_mode(self, mock_user, mock_git_service):
        """Without write mode active, git_merge is rejected."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = False
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "write mode" in data["error"].lower()
                or "write_mode" in data["error"].lower()
            )

    def test_handler_succeeds_with_write_mode_active(self, mock_user, mock_git_service):
        """With write mode active, git_merge proceeds."""
        mock_git_service.merge_branch.return_value = {
            "success": True,
            "merge_summary": "Already up to date.",
            "conflicts": [],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["conflicts"] == []


class TestGitMergeHandlerAccessControl:
    """Test access control (invisible repo pattern) in git_merge handler."""

    def test_handler_group_access_invisible_repo(self, mock_user):
        """User without group access gets 'Repository not found' error."""
        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (
                None,
                "Repository not found or access denied",
            )

            params = {
                "repository_alias": "private-repo",
                "source_branch": "feature",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "not found" in data["error"].lower()
                or "denied" in data["error"].lower()
            )


class TestGitMergeHandlerCleanMerge:
    """Test clean (no-conflict) merge response."""

    def test_handler_clean_merge_success(self, mock_user, mock_git_service):
        """Clean merge returns success=True with empty conflicts list."""
        mock_git_service.merge_branch.return_value = {
            "success": True,
            "merge_summary": "Merge made by the 'ort' strategy.",
            "conflicts": [],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature/login",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["conflicts"] == []
            assert "merge_summary" in data

    def test_handler_passes_correct_args_to_service(self, mock_user, mock_git_service):
        """Handler passes repo path and source_branch to service correctly."""
        from pathlib import Path

        mock_git_service.merge_branch.return_value = {
            "success": True,
            "merge_summary": "Already up to date.",
            "conflicts": [],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature/login",
            }

            handlers.git_merge(params, mock_user)

            mock_git_service.merge_branch.assert_called_once_with(
                Path("/tmp/test-repo"), "feature/login"
            )


class TestGitMergeHandlerConflictMerge:
    """Test conflict merge response structure."""

    def test_handler_conflict_merge_returns_conflicts(
        self, mock_user, mock_git_service
    ):
        """Conflict merge returns success=False with conflict list."""
        mock_git_service.merge_branch.return_value = {
            "success": False,
            "merge_summary": "CONFLICT (content): Merge conflict in src/app.py",
            "conflicts": [
                {
                    "file": "src/app.py",
                    "status": "UU",
                    "conflict_type": "content",
                    "is_binary": False,
                }
            ],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert len(data["conflicts"]) == 1
            conflict = data["conflicts"][0]
            assert conflict["file"] == "src/app.py"
            assert conflict["status"] == "UU"
            assert conflict["conflict_type"] == "content"
            assert conflict["is_binary"] is False

    def test_handler_returns_binary_conflict(self, mock_user, mock_git_service):
        """Binary file conflicts have is_binary=True in response."""
        mock_git_service.merge_branch.return_value = {
            "success": False,
            "merge_summary": "CONFLICT (content): Merge conflict in data.bin",
            "conflicts": [
                {
                    "file": "data.bin",
                    "status": "UU",
                    "conflict_type": "content",
                    "is_binary": True,
                }
            ],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "feature",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert data["conflicts"][0]["is_binary"] is True


class TestGitMergeHandlerErrors:
    """Test error handling in git_merge handler."""

    def test_handler_git_command_error_returns_structured_error(
        self, mock_user, mock_git_service
    ):
        """GitCommandError is returned as structured error response."""
        error = GitCommandError(
            message="git merge failed: invalid branch",
            stderr="fatal: bad object: nonexistent",
            returncode=128,
            command=["git", "merge", "nonexistent"],
        )
        mock_git_service.merge_branch.side_effect = error

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "source_branch": "nonexistent",
            }

            mcp_response = handlers.git_merge(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert data["error_type"] == "GitCommandError"
            assert "stderr" in data
