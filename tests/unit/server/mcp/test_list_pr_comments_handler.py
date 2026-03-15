"""
Unit tests for list_pull_request_comments MCP handler.

Story #448: list_pull_request_comments - Read review comments and threads

Tests:
  - GitHub PR comments success via handler
  - GitLab MR notes success via handler
  - Missing repository_alias -> error
  - Missing number -> error
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


def _make_unified_comment(
    id=101,
    author="reviewer1",
    body="Test comment",
    file_path="src/auth.py",
    line_number=42,
    is_review_comment=True,
    resolved=None,
):
    """Build a normalized comment dict as returned by forge client."""
    return {
        "id": id,
        "author": author,
        "body": body,
        "created_at": "2026-03-11T10:00:00Z",
        "updated_at": "2026-03-11T10:00:00Z",
        "file_path": file_path,
        "line_number": line_number,
        "is_review_comment": is_review_comment,
        "resolved": resolved,
    }


class TestListPullRequestCommentsHandler:
    """Tests for list_pull_request_comments MCP handler (Story #448)."""

    def test_handler_calls_github_client(self, mock_user):
        """Handler calls GitHubForgeClient.list_pull_request_comments and returns results."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@github.com:myorg/myrepo.git"
        mock_comments = [
            _make_unified_comment(id=101, body="Please add error handling"),
            _make_unified_comment(
                id=201,
                file_path=None,
                line_number=None,
                is_review_comment=False,
                body="LGTM overall",
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_request_comments"
            ) as mock_list_comments,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_comments.return_value = mock_comments

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                "limit": 50,
            }

            mcp_response = handlers.list_pull_request_comments(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert len(data["comments"]) == 2
            assert data["comments"][0]["id"] == 101
            assert data["forge_type"] == "github"

    def test_handler_calls_gitlab_client(self, mock_user):
        """Handler calls GitLabForgeClient.list_merge_request_notes for GitLab repos."""
        from code_indexer.server.mcp import handlers

        mock_remote_url = "git@gitlab.com:myorg/myrepo.git"
        mock_notes = [
            _make_unified_comment(id=301, body="Needs refactoring"),
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
                "code_indexer.server.clients.forge_client.GitLabForgeClient.list_merge_request_notes"
            ) as mock_list_notes,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {"token": "glpat_testtoken"},
                mock_remote_url,
                None,
            )
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_notes.return_value = mock_notes

            params = {
                "repository_alias": "test-repo-gitlab",
                "number": 7,
                "limit": 50,
            }

            mcp_response = handlers.list_pull_request_comments(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert len(data["comments"]) == 1
            assert data["comments"][0]["id"] == 301
            assert data["forge_type"] == "gitlab"

    def test_handler_missing_number(self, mock_user):
        """Missing 'number' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "repository_alias": "test-repo",
            # number is missing
        }

        mcp_response = handlers.list_pull_request_comments(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "number" in data["error"]

    def test_handler_missing_alias(self, mock_user):
        """Missing 'repository_alias' parameter returns error."""
        from code_indexer.server.mcp import handlers

        params = {
            "number": 42,
        }

        mcp_response = handlers.list_pull_request_comments(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_registered(self):
        """list_pull_request_comments is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "list_pull_request_comments" in HANDLER_REGISTRY

    def test_handler_default_limit_is_50(self, mock_user):
        """Handler uses limit=50 as default when not specified."""
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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_request_comments"
            ) as mock_list_comments,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = ({"token": "ghp_testtoken"}, mock_remote_url, None)
            mock_run.return_value = MagicMock(stdout=mock_remote_url, returncode=0)
            mock_list_comments.return_value = []

            params = {
                "repository_alias": "test-repo",
                "number": 42,
                # no limit - should default to 50
            }

            handlers.list_pull_request_comments(params, mock_user)

            call_kwargs = mock_list_comments.call_args[1]
            assert call_kwargs.get("limit") == 50

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
                "code_indexer.server.clients.forge_client.GitHubForgeClient.list_pull_request_comments",
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

            mcp_response = handlers.list_pull_request_comments(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "auth" in data["error"].lower()
                or "token" in data["error"].lower()
                or "Invalid" in data["error"]
            )
