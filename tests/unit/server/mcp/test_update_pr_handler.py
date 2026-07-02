"""
Unit tests for update_pull_request MCP handler.

Story #450: update_pull_request - Update PR/MR metadata

Tests:
  - GitHub PR update success via handler
  - GitLab MR update success via handler
  - Missing repository_alias -> error
  - Missing number -> error
  - No fields provided -> validation error
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


class TestUpdatePullRequestHandler:
    """Tests for update_pull_request MCP handler (Story #450)."""

    def test_handler_github_update_title_success(self, mock_user):
        """Handler calls GitHubForgeClient.update_pull_request and returns results."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "url": "https://github.com/myorg/myrepo/pull/42",
            "updated_fields": ["title"],
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.update_pull_request"
            ) as mock_update,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_update.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "title": "New PR Title",
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert "url" in data
            assert data["forge_type"] == "github"
            assert "title" in data["updated_fields"]

    def test_handler_github_update_multiple_fields(self, mock_user):
        """Handler passes all provided fields to forge client."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "url": "https://github.com/myorg/myrepo/pull/42",
            "updated_fields": sorted(["title", "description", "labels"]),
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.update_pull_request"
            ) as mock_update,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_update.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "title": "New Title",
                "description": "Updated body",
                "labels": ["bug", "enhancement"],
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["forge_type"] == "github"

            # Verify all fields were passed to forge client
            call_kwargs = mock_update.call_args[1]
            assert call_kwargs.get("title") == "New Title"
            assert call_kwargs.get("description") == "Updated body"
            assert call_kwargs.get("labels") == ["bug", "enhancement"]

    def test_handler_gitlab_update_success(self, mock_user):
        """Handler calls GitLabForgeClient.update_merge_request for GitLab repos."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "url": "https://gitlab.com/myorg/myrepo/-/merge_requests/7",
            "updated_fields": ["title"],
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
                "code_indexer.server.clients.forge_client.GitLabForgeClient.update_merge_request"
            ) as mock_update,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat_testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_update.return_value = mock_result

            params = {
                "repository_alias": "test-repo-gitlab",
                "number": 7,
                "title": "Updated MR Title",
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_repository_alias(self, mock_user):
        """Missing 'repository_alias' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "number": 42,
            "title": "New Title",
        }

        mcp_response = handlers.update_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_missing_number(self, mock_user):
        """Missing 'number' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "title": "New Title",
        }

        mcp_response = handlers.update_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "number" in data["error"]

    def test_handler_no_fields_provided_returns_error(self, mock_user):
        """Providing no update fields (no title, description, labels, etc.) returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "number": 42,
            # No title, description, labels, assignees, or reviewers
        }

        mcp_response = handlers.update_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert (
            "field" in data["error"].lower()
            or "title" in data["error"].lower()
            or "update" in data["error"].lower()
        )

    def test_handler_registered(self):
        """update_pull_request is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "update_pull_request" in HANDLER_REGISTRY

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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.update_pull_request",
                side_effect=ForgeAuthenticationError("Invalid token"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "bad_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "title": "New Title",
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
            )

    def test_handler_value_error_returns_error(self, mock_user):
        """ValueError from forge client (e.g. PR not found) returns error response."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.update_pull_request",
                side_effect=ValueError("PR #99999 not found"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 99999,
                "title": "New Title",
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"].lower()

    def test_handler_reviewers_passed_to_github_client(self, mock_user):
        """reviewers parameter is forwarded to the GitHub forge client."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_result = {
            "success": True,
            "url": "https://github.com/myorg/myrepo/pull/42",
            "updated_fields": ["reviewers"],
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.update_pull_request"
            ) as mock_update,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_update.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "reviewers": ["alice", "bob"],
            }

            mcp_response = handlers.update_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            call_kwargs = mock_update.call_args[1]
            assert call_kwargs.get("reviewers") == ["alice", "bob"]
