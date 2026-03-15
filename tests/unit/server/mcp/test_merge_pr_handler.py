"""
Unit tests for merge_pull_request MCP handler.

Story #451: merge_pull_request - Merge a GitHub PR or GitLab MR

Tests:
  - GitHub PR merge success via handler
  - GitLab MR merge success via handler
  - Missing repository_alias -> error
  - Missing number -> error
  - Handler registered in HANDLER_REGISTRY
  - ForgeAuthenticationError returns error response
  - delete_branch=True triggers branch deletion
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


class TestMergePullRequestHandler:
    """Tests for merge_pull_request MCP handler (Story #451)."""

    def test_handler_registered_in_handler_registry(self):
        """merge_pull_request is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "merge_pull_request" in HANDLER_REGISTRY

    def test_handler_github_merge_success(self, mock_user):
        """Handler calls GitHubForgeClient.merge_pull_request and returns success."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "merged": True,
            "sha": "abc123",
            "message": "PR #42 merged",
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.merge_pull_request"
            ) as mock_merge,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_merge.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
            }

            mcp_response = handlers.merge_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["merged"] is True
            assert data["forge_type"] == "github"

    def test_handler_gitlab_merge_success(self, mock_user):
        """Handler calls GitLabForgeClient.merge_merge_request and returns success."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "merged": True,
            "sha": "def456",
            "message": "MR #5 merged",
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
                "code_indexer.server.clients.forge_client.GitLabForgeClient.merge_merge_request"
            ) as mock_merge,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat-testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_merge.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 5,
            }

            mcp_response = handlers.merge_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_repository_alias_returns_error(self, mock_user):
        """Handler returns error when repository_alias is missing."""
        from code_indexer.server.mcp import handlers

        params = {"number": 42}

        mcp_response = handlers.merge_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_missing_number_returns_error(self, mock_user):
        """Handler returns error when number is missing."""
        from code_indexer.server.mcp import handlers

        params = {"repository_alias": "test-repo"}

        mcp_response = handlers.merge_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "number" in data["error"]

    def test_handler_forge_auth_error_returns_error_response(self, mock_user):
        """ForgeAuthenticationError is caught and returned as error response."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.merge_pull_request"
            ) as mock_merge,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "bad_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_merge.side_effect = ForgeAuthenticationError("Invalid token")

            params = {
                "repository_alias": "test-repo",
                "number": 42,
            }

            mcp_response = handlers.merge_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "Invalid token" in data["error"]

    def test_handler_passes_merge_method_to_client(self, mock_user):
        """Handler passes merge_method param to forge client."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "merged": True,
            "sha": "abc123",
            "message": "PR #42 merged",
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.merge_pull_request"
            ) as mock_merge,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_merge.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "merge_method": "squash",
            }

            handlers.merge_pull_request(params, mock_user)

            call_kwargs = mock_merge.call_args[1]
            assert call_kwargs.get("merge_method") == "squash"

    def test_handler_repo_not_found_returns_error(self, mock_user):
        """Handler returns error when repository cannot be resolved."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository 'unknown' not found")

            params = {
                "repository_alias": "unknown",
                "number": 42,
            }

            mcp_response = handlers.merge_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"]
