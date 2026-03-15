"""
Unit tests for get_pull_request MCP handler.

Story #447: get_pull_request - Get full PR/MR details

Tests:
  - GitHub PR fetch success via handler
  - GitLab MR fetch success via handler
  - Missing repository_alias -> error
  - Missing number -> error
  - Repo resolution failure -> error
  - Auth failure -> appropriate error
  - PR not found (ValueError from client) -> error
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
    number=42,
    title="My PR",
    description="PR description",
    state="open",
    author="octocat",
    source_branch="feature/x",
    target_branch="main",
    url=None,
    labels=None,
    reviewers=None,
    mergeable=True,
    ci_status="clean",
    diff_stats=None,
):
    """Build a fully normalized PR dict as returned by forge client."""
    return {
        "number": number,
        "title": title,
        "description": description,
        "state": state,
        "author": author,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "url": url or f"https://github.com/owner/repo/pull/{number}",
        "labels": labels if labels is not None else [],
        "reviewers": reviewers if reviewers is not None else [],
        "mergeable": mergeable,
        "ci_status": ci_status,
        "diff_stats": diff_stats
        or {"additions": 10, "deletions": 5, "changed_files": 2},
        "created_at": "2026-03-10T14:30:00Z",
        "updated_at": "2026-03-12T09:15:00Z",
    }


class TestGetPullRequestHandler:
    """Tests for get_pull_request MCP handler (Story #447)."""

    def test_handler_calls_github_client(self, mock_user):
        """Handler calls GitHubForgeClient.get_pull_request and returns result."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_pr = _make_normalized_pr(number=42, title="My PR")

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitHubForgeClient.get_pull_request"
            ) as mock_get_pr,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_get_pr.return_value = mock_pr

            params = {
                "repository_alias": "test-repo",
                "number": 42,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["pull_request"]["number"] == 42
            assert data["pull_request"]["title"] == "My PR"
            assert data["forge_type"] == "github"

    def test_handler_calls_gitlab_client(self, mock_user):
        """Handler calls GitLabForgeClient.get_merge_request for GitLab repos."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_mr = _make_normalized_pr(
            number=7,
            title="My MR",
            url="https://gitlab.com/myorg/myrepo/-/merge_requests/7",
        )

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch("subprocess.run") as mock_run,
            patch(
                "code_indexer.server.clients.forge_client.GitLabForgeClient.get_merge_request"
            ) as mock_get_mr,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat_testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_get_mr.return_value = mock_mr

            params = {
                "repository_alias": "test-repo-gitlab",
                "number": 7,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["pull_request"]["number"] == 7
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_alias(self, mock_user):
        """Missing repository_alias returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "number": 42,
        }

        mcp_response = handlers.get_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_missing_number(self, mock_user):
        """Missing number returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
        }

        mcp_response = handlers.get_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "number" in data["error"]

    def test_handler_repo_not_found(self, mock_user):
        """Repo resolution failure returns error."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository 'missing-repo' not found.")

            params = {
                "repository_alias": "missing-repo",
                "number": 42,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"]

    def test_handler_pr_not_found(self, mock_user):
        """ValueError from client (PR not found) returns error response."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.get_pull_request",
                side_effect=ValueError("PR #9999 not found"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 9999,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "9999" in data["error"] or "not found" in data["error"].lower()

    def test_handler_auth_failure_returns_error(self, mock_user):
        """ForgeAuthenticationError from client returns error."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.get_pull_request",
                side_effect=ForgeAuthenticationError("Invalid token"),
            ),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "bad_token"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)

            params = {
                "repository_alias": "test-repo",
                "number": 42,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
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
                "number": 42,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "credential" in data["error"].lower()
                or "No credentials" in data["error"]
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
                "number": 42,
            }

            mcp_response = handlers.get_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "forge" in data["error"].lower()
                or "unsupported" in data["error"].lower()
            )

    def test_handler_registered(self):
        """get_pull_request is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "get_pull_request" in HANDLER_REGISTRY
