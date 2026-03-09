"""
Unit tests for git_conflict_status and git_mark_resolved MCP handlers (Story #389).

Tests handler parameter validation, write-mode enforcement,
and proper service delegation following the git_merge_handler pattern.
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
# git_conflict_status handler tests
# ---------------------------------------------------------------------------


class TestGitConflictStatusHandler:
    """Tests for git_conflict_status handler."""

    def test_requires_repository_alias(self, mock_user):
        """Missing repository_alias returns error without alias."""
        mcp_response = handlers.git_conflict_status({}, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_no_write_mode_needed(self, mock_user, mock_git_service):
        """git_conflict_status succeeds without write mode active."""
        mock_git_service.git_conflict_status.return_value = {
            "in_merge": False,
            "conflicted_files": [],
            "total_conflicts": 0,
        }

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = ("/tmp/test-repo", None)

            params = {"repository_alias": "test-repo"}
            mcp_response = handlers.git_conflict_status(params, mock_user)
            data = _extract_response_data(mcp_response)

        # Should succeed — no write mode check for read-only handler
        assert "error" not in data or data.get("success") is not False

    def test_returns_conflict_status_result(self, mock_user, mock_git_service):
        """Handler returns the service result."""
        expected = {
            "in_merge": True,
            "conflicted_files": [
                {
                    "file": "src/app.py",
                    "status": "UU",
                    "regions": [
                        {
                            "start_line": 10,
                            "end_line": 16,
                            "ours_label": "HEAD",
                            "theirs_label": "feature",
                            "ours_content": "old",
                            "theirs_content": "new",
                        }
                    ],
                    "is_binary": False,
                }
            ],
            "total_conflicts": 1,
        }
        mock_git_service.git_conflict_status.return_value = expected

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = ("/tmp/test-repo", None)

            params = {"repository_alias": "test-repo"}
            mcp_response = handlers.git_conflict_status(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["in_merge"] is True
        assert data["total_conflicts"] == 1
        assert len(data["conflicted_files"]) == 1

    def test_returns_error_on_repo_resolution_failure(self, mock_user):
        """When repo resolution fails, error is returned."""
        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository not found")

            params = {"repository_alias": "nonexistent-repo"}
            mcp_response = handlers.git_conflict_status(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "not found" in data["error"].lower() or "error" in data

    def test_git_command_error_returns_structured_error(
        self, mock_user, mock_git_service
    ):
        """GitCommandError is caught and returned as structured error."""
        mock_git_service.git_conflict_status.side_effect = GitCommandError(
            "git status failed", stderr="fatal: not a repo", returncode=128
        )

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = ("/tmp/test-repo", None)

            params = {"repository_alias": "test-repo"}
            mcp_response = handlers.git_conflict_status(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert data["error_type"] == "GitCommandError"


# ---------------------------------------------------------------------------
# git_mark_resolved handler tests
# ---------------------------------------------------------------------------


class TestGitMarkResolvedHandler:
    """Tests for git_mark_resolved handler."""

    def test_requires_repository_alias(self, mock_user):
        """Missing repository_alias returns error."""
        params = {"file_path": "src/app.py"}
        mcp_response = handlers.git_mark_resolved(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_requires_file_path(self, mock_user):
        """Missing file_path returns error."""
        params = {"repository_alias": "test-repo"}
        mcp_response = handlers.git_mark_resolved(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "file_path" in data["error"]

    def test_enforces_write_mode(self, mock_user, mock_git_service):
        """Without write mode active, git_mark_resolved is rejected."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = False
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "file_path": "src/app.py",
            }
            mcp_response = handlers.git_mark_resolved(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert (
            "write mode" in data["error"].lower()
            or "write_mode" in data["error"].lower()
        )

    def test_returns_result_when_write_mode_active(self, mock_user, mock_git_service):
        """With write mode active, returns service result."""
        expected = {
            "success": True,
            "file": "src/app.py",
            "remaining_conflicts": 0,
            "all_resolved": True,
            "message": "All conflicts resolved. Run git_commit to complete the merge.",
        }
        mock_git_service.git_mark_resolved.return_value = expected

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "file_path": "src/app.py",
            }
            mcp_response = handlers.git_mark_resolved(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is True
        assert data["all_resolved"] is True
        assert data["remaining_conflicts"] == 0

    def test_value_error_returns_error_response(self, mock_user, mock_git_service):
        """ValueError from service (markers still present) returns error."""
        mock_git_service.git_mark_resolved.side_effect = ValueError(
            "File still contains conflict markers."
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "file_path": "src/app.py",
            }
            mcp_response = handlers.git_mark_resolved(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "conflict markers" in data["error"]

    def test_git_command_error_returns_structured_error(
        self, mock_user, mock_git_service
    ):
        """GitCommandError returns structured error response."""
        mock_git_service.git_mark_resolved.side_effect = GitCommandError(
            "git add failed", stderr="error: pathspec not found", returncode=1
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._is_write_mode_active"
            ) as mock_write_mode,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_write_mode.return_value = True
            mock_app.app.state.golden_repos_dir = "/tmp/golden-repos"

            params = {
                "repository_alias": "test-repo",
                "file_path": "src/app.py",
            }
            mcp_response = handlers.git_mark_resolved(params, mock_user)
            data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert data["error_type"] == "GitCommandError"
