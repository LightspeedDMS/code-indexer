"""
Unit tests for comment_on_pull_request MCP handler.

Story #449: comment_on_pull_request - Add comments to PR/MR

Tests:
  - GitHub PR general comment success via handler
  - GitHub PR inline comment success via handler
  - GitLab MR general comment success via handler
  - Missing repository_alias -> error
  - Missing number -> error
  - Missing body -> error
  - file_path provided but not line_number -> validation error
  - Handler registered in HANDLER_REGISTRY
  - ForgeAuthenticationError returns error response
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Standard test user."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    content = mcp_response["content"][0]
    return cast(dict, json.loads(content["text"]))


class TestCommentOnPullRequestHandler:
    """Tests for comment_on_pull_request MCP handler (Story #449)."""

    def test_handler_github_general_comment_success(self, mock_user):
        """Handler calls GitHubForgeClient.comment_on_pull_request and returns results."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "comment_id": 9001,
            "url": "https://github.com/myorg/myrepo/pull/42#issuecomment-9001",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.comment_on_pull_request"
            ) as mock_comment,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_comment.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "body": "This looks good!",
            }

            mcp_response = handlers.comment_on_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["comment_id"] == 9001
            assert "url" in data
            assert data["forge_type"] == "github"

    def test_handler_github_inline_comment_success(self, mock_user):
        """Handler passes file_path and line_number to forge client for inline comments."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "comment_id": 9002,
            "url": "https://github.com/myorg/myrepo/pull/42#discussion_r9002",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.comment_on_pull_request"
            ) as mock_comment,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_comment.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "body": "Consider renaming this variable",
                "file_path": "src/auth.py",
                "line_number": 55,
            }

            mcp_response = handlers.comment_on_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["comment_id"] == 9002
            assert data["forge_type"] == "github"

            # Verify file_path and line_number were passed to forge client
            call_kwargs = mock_comment.call_args[1]
            assert call_kwargs.get("file_path") == "src/auth.py"
            assert call_kwargs.get("line_number") == 55

    def test_handler_gitlab_general_comment_success(self, mock_user):
        """Handler calls GitLabForgeClient.comment_on_merge_request for GitLab repos."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_result = {
            "comment_id": 5001,
            "url": "https://gitlab.com/myorg/myrepo/-/merge_requests/7#note_5001",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitLabForgeClient.comment_on_merge_request"
            ) as mock_comment,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat_testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_comment.return_value = mock_result

            params = {
                "repository_alias": "test-repo-gitlab",
                "number": 7,
                "body": "LGTM!",
            }

            mcp_response = handlers.comment_on_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["comment_id"] == 5001
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_repository_alias(self, mock_user):
        """Missing 'repository_alias' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "number": 42,
            "body": "Some comment",
        }

        mcp_response = handlers.comment_on_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_missing_number(self, mock_user):
        """Missing 'number' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "body": "Some comment",
        }

        mcp_response = handlers.comment_on_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "number" in data["error"]

    def test_handler_missing_body(self, mock_user):
        """Missing 'body' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "number": 42,
        }

        mcp_response = handlers.comment_on_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "body" in data["error"]

    def test_handler_file_path_without_line_number_returns_error(self, mock_user):
        """Providing file_path without line_number returns a validation error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "number": 42,
            "body": "Inline comment",
            "file_path": "src/auth.py",
            # line_number is missing
        }

        mcp_response = handlers.comment_on_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "line_number" in data["error"] or "file_path" in data["error"]

    def test_handler_registered(self):
        """comment_on_pull_request is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "comment_on_pull_request" in HANDLER_REGISTRY

    def test_handler_auth_failure_returns_error(self, mock_user):
        """ForgeAuthenticationError from API returns error response."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.clients.forge_client import ForgeAuthenticationError

        mock_remote_url = "git@github.com:myorg/myrepo.git"

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.comment_on_pull_request",
                side_effect=ForgeAuthenticationError("Invalid token"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "bad_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "body": "test comment",
            }

            mcp_response = handlers.comment_on_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
            )

    def test_handler_value_error_returns_error(self, mock_user):
        """ValueError from forge client returns error response."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.comment_on_pull_request",
                side_effect=ValueError("PR not found"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 99999,
                "body": "test comment",
            }

            mcp_response = handlers.comment_on_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "PR not found" in data["error"]
