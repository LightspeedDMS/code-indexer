"""
Unit tests for list_pull_requests MCP handler.

Story #446: list_pull_requests - List PRs/MRs for a repository

Tests:
  - GitHub PR listing success via handler
  - GitLab MR listing success via handler
  - Missing repository_alias -> error
  - Repo resolution failure -> error
  - Auth failure -> appropriate error
  - Handler registered in HANDLER_REGISTRY
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Standard test user with query_repos permission."""
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


def _make_normalized_pr(
    number=1,
    title="Test PR",
    state="open",
    author="octocat",
    source_branch="feature",
    target_branch="main",
    url=None,
):
    """Build a normalized PR dict as returned by forge client."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "author": author,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "url": url or f"https://github.com/owner/repo/pull/{number}",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }


class TestListPullRequestsHandler:
    """Tests for list_pull_requests MCP handler (Story #446)."""

    def test_handler_calls_github_client(self, mock_user):
        """Handler calls GitHubForgeClient.list_pull_requests and returns results."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_prs = [
            _make_normalized_pr(number=1, title="First PR"),
            _make_normalized_pr(number=2, title="Second PR"),
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_requests"
            ) as mock_list_prs,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_prs.return_value = mock_prs

            params = {
                "repository_alias": "test-repo",
                "state": "open",
                "limit": 10,
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert len(data["pull_requests"]) == 2
            assert data["pull_requests"][0]["number"] == 1
            assert data["pull_requests"][0]["title"] == "First PR"
            assert data["forge_type"] == "github"

    def test_handler_calls_gitlab_client(self, mock_user):
        """Handler calls GitLabForgeClient.list_merge_requests for GitLab repos."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_mrs = [
            _make_normalized_pr(
                number=7,
                title="My MR",
                url="https://gitlab.com/myorg/myrepo/-/merge_requests/7",
            ),
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitLabForgeClient.list_merge_requests"
            ) as mock_list_mrs,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat_testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_mrs.return_value = mock_mrs

            params = {
                "repository_alias": "test-repo-gitlab",
                "state": "open",
                "limit": 10,
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert len(data["pull_requests"]) == 1
            assert data["pull_requests"][0]["number"] == 7
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_alias(self, mock_user):
        """Missing repository_alias returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "state": "open",
        }

        mcp_response = handlers.list_pull_requests(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_repo_not_found(self, mock_user):
        """Repo resolution failure returns error."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository 'missing-repo' not found.")

            params = {
                "repository_alias": "missing-repo",
                "state": "open",
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"]

    def test_handler_auth_failure_returns_error(self, mock_user):
        """ForgeAuthenticationError from API returns error."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_requests",
                side_effect=ForgeAuthenticationError("Invalid token"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "bad_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "state": "open",
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
            )

    def test_handler_unknown_forge_type_returns_error(self, mock_user):
        """Remote URL with unknown forge type returns error."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@bitbucket.org:myorg/myrepo.git"

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "some_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "state": "open",
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "forge" in data["error"].lower()
                or "unsupported" in data["error"].lower()
            )

    def test_handler_cred_error_returns_error(self, mock_user):
        """Credential resolution error is propagated as error response."""
        from code_indexer.server.mcp import handlers

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (None, None, "No credentials found for origin")

            params = {
                "repository_alias": "test-repo",
                "state": "open",
            }

            mcp_response = handlers.list_pull_requests(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "credential" in data["error"].lower()
                or "No credentials" in data["error"]
            )

    def test_handler_default_state_is_open(self, mock_user):
        """Handler uses 'open' as default state when not specified."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_requests"
            ) as mock_list_prs,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_prs.return_value = []

            params = {
                "repository_alias": "test-repo",
                # no state specified - should default to "open"
            }

            handlers.list_pull_requests(params, mock_user)

            # Verify it was called with state="open"
            call_kwargs = mock_list_prs.call_args[1]
            assert call_kwargs.get("state") == "open"

    def test_handler_registered(self):
        """list_pull_requests is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "list_pull_requests" in HANDLER_REGISTRY
