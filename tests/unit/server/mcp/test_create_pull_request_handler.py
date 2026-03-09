"""
Unit tests for create_pull_request MCP handler.

Story #390: Pull/Merge Request Creation via MCP

Tests:
  - GitHub PR creation success
  - GitLab MR creation success
  - Write mode not active -> error
  - Unknown forge type -> error
  - Auth failure -> appropriate error
  - Missing required params -> error
  - Repo resolution failure -> error
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Standard test user with power_user role (has repository:write)."""
    return User(
        username="testuser",
        role=UserRole.POWER_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    content = mcp_response["content"][0]
    return cast(dict, json.loads(content["text"]))


class TestCreatePullRequestHandler:
    """Tests for create_pull_request MCP handler (Story #390)."""

    def test_github_pr_success(self, mock_user):
        """Successful GitHub PR creation returns pr_url and pr_number."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=True
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ), patch(
            "subprocess.run"
        ) as mock_run, patch(
            "code_indexer.server.clients.forge_client.GitHubForgeClient.create_pull_request"
        ) as mock_create_pr:
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_run.return_value = MagicMock(
                stdout=mock_remote_url, returncode=0
            )
            mock_create_pr.return_value = {
                "url": "https://github.com/myorg/myrepo/pull/42",
                "number": 42,
            }

            params = {
                "repository_alias": "test-repo",
                "title": "My PR",
                "body": "Description",
                "head": "feature-branch",
                "base": "main",
                "token": "ghp_testtoken",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["pr_url"] == "https://github.com/myorg/myrepo/pull/42"
            assert data["pr_number"] == 42
            assert data["forge_type"] == "github"

    def test_gitlab_mr_success(self, mock_user):
        """Successful GitLab MR creation returns pr_url and pr_number."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=True
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ), patch(
            "subprocess.run"
        ) as mock_run, patch(
            "code_indexer.server.clients.forge_client.GitLabForgeClient.create_merge_request"
        ) as mock_create_mr:
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_run.return_value = MagicMock(
                stdout=mock_remote_url, returncode=0
            )
            mock_create_mr.return_value = {
                "url": "https://gitlab.com/myorg/myrepo/-/merge_requests/7",
                "number": 7,
            }

            params = {
                "repository_alias": "test-repo-gitlab",
                "title": "My MR",
                "body": "Description",
                "head": "feature-branch",
                "base": "main",
                "token": "glpat_testtoken",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["pr_url"] == "https://gitlab.com/myorg/myrepo/-/merge_requests/7"
            assert data["pr_number"] == 7
            assert data["forge_type"] == "gitlab"

    def test_write_mode_not_active_returns_error(self, mock_user):
        """When write mode is not active, returns error (AC4)."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=False
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)

            params = {
                "repository_alias": "test-repo",
                "title": "My PR",
                "body": "",
                "head": "feature",
                "base": "main",
                "token": "ghp_testtoken",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "write mode" in data["error"].lower()

    def test_missing_repository_alias_returns_error(self, mock_user):
        """Missing repository_alias parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "title": "My PR",
            "head": "feature",
            "base": "main",
            "token": "ghp_testtoken",
        }

        mcp_response = handlers.create_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_missing_title_returns_error(self, mock_user):
        """Missing title parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "head": "feature",
            "base": "main",
            "token": "ghp_testtoken",
        }

        mcp_response = handlers.create_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "title" in data["error"]

    def test_missing_head_returns_error(self, mock_user):
        """Missing head parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "title": "My PR",
            "base": "main",
            "token": "ghp_testtoken",
        }

        mcp_response = handlers.create_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "head" in data["error"]

    def test_missing_base_returns_error(self, mock_user):
        """Missing base parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "title": "My PR",
            "head": "feature",
            "token": "ghp_testtoken",
        }

        mcp_response = handlers.create_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "base" in data["error"]

    def test_missing_token_returns_error(self, mock_user):
        """Missing token parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            "title": "My PR",
            "head": "feature",
            "base": "main",
        }

        mcp_response = handlers.create_pull_request(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "token" in data["error"]

    def test_repo_not_found_returns_error(self, mock_user):
        """Repo path resolution failure returns error."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository 'missing-repo' not found.")

            params = {
                "repository_alias": "missing-repo",
                "title": "My PR",
                "head": "feature",
                "base": "main",
                "token": "ghp_testtoken",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"]

    def test_unknown_forge_type_returns_error(self, mock_user):
        """Remote URL with unknown forge type returns error (AC3)."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@bitbucket.org:myorg/myrepo.git"

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=True
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ), patch(
            "subprocess.run"
        ) as mock_run:
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_run.return_value = MagicMock(
                stdout=mock_remote_url, returncode=0
            )

            params = {
                "repository_alias": "test-repo",
                "title": "My PR",
                "head": "feature",
                "base": "main",
                "token": "some_token",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "forge" in data["error"].lower() or "unsupported" in data["error"].lower()

    def test_auth_failure_returns_error(self, mock_user):
        """ForgeAuthenticationError from API returns error (AC5)."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.clients.forge_client import ForgeAuthenticationError

        mock_remote_url = "git@github.com:myorg/myrepo.git"

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=True
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ), patch(
            "subprocess.run"
        ) as mock_run, patch(
            "code_indexer.server.clients.forge_client.GitHubForgeClient.create_pull_request",
            side_effect=ForgeAuthenticationError("Invalid token"),
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_run.return_value = MagicMock(
                stdout=mock_remote_url, returncode=0
            )

            params = {
                "repository_alias": "test-repo",
                "title": "My PR",
                "head": "feature",
                "base": "main",
                "token": "bad_token",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
            )

    def test_extract_owner_repo_failure_returns_error(self, mock_user):
        """Forge is detected (github) but extract_owner_repo fails -> error returned."""
        from code_indexer.server.mcp import handlers

        # URL has github hostname so forge detection succeeds, but no owner/repo path
        mock_remote_url = "https://github.com"

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve, patch(
            "code_indexer.server.mcp.handlers._is_write_mode_active", return_value=True
        ), patch(
            "code_indexer.server.mcp.handlers._get_golden_repos_dir", return_value="/tmp/golden"
        ), patch(
            "subprocess.run"
        ) as mock_run:
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_run.return_value = MagicMock(
                stdout=mock_remote_url, returncode=0
            )

            params = {
                "repository_alias": "test-repo",
                "title": "My PR",
                "body": "",
                "head": "feature",
                "base": "main",
                "token": "ghp_testtoken",
            }

            mcp_response = handlers.create_pull_request(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert data.get("error")

    def test_handler_registered_in_registry(self):
        """create_pull_request is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "create_pull_request" in HANDLER_REGISTRY
