"""
Unit tests for git_amend MCP handler.

Story #454: git_amend - Amend the most recent git commit

Tests:
  - amend with message success
  - amend without message success (--no-edit)
  - Missing repository_alias -> error
  - Handler registered in HANDLER_REGISTRY
  - PAT credential identity used for author/committer env vars
  - GitCommandError returns error response
  - Returns commit_hash in response
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch

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


class TestGitAmendHandler:
    """Tests for git_amend MCP handler (Story #454)."""

    def test_handler_registered_in_handler_registry(self):
        """git_amend is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "git_amend" in HANDLER_REGISTRY

    def test_handler_missing_repository_alias_returns_error(self, mock_user):
        """Handler returns error when repository_alias is missing."""
        from code_indexer.server.mcp import handlers

        params = {"message": "New message"}

        mcp_response = handlers.git_amend(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_amend_with_message_returns_success(self, mock_user):
        """Handler calls git_amend with message and returns success."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "commit_hash": "abc123def456",
            "message": "Amended commit",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {
                    "token": "ghp_testtoken",
                    "git_user_name": "Test User",
                    "git_user_email": "test@example.com",
                },
                "git@github.com:org/repo.git",
                None,
            )
            mock_service.git_amend.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "message": "Fix typo in commit message",
            }

            mcp_response = handlers.git_amend(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True
            assert data["commit_hash"] == "abc123def456"

    def test_handler_amend_without_message_returns_success(self, mock_user):
        """Handler calls git_amend without message (--no-edit) and returns success."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "commit_hash": "newcommithash123",
            "message": "Amended commit (no message change)",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {
                    "token": "ghp_testtoken",
                    "git_user_name": "Test User",
                    "git_user_email": "test@example.com",
                },
                "git@github.com:org/repo.git",
                None,
            )
            mock_service.git_amend.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                # No message -> --no-edit
            }

            mcp_response = handlers.git_amend(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is True

    def test_handler_passes_env_with_git_identity_from_pat(self, mock_user):
        """Handler sets GIT_AUTHOR/COMMITTER env from PAT credential identity."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "commit_hash": "abc123",
            "message": "Amended",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {
                    "token": "ghp_testtoken",
                    "git_user_name": "Jane Doe",
                    "git_user_email": "jane@example.com",
                },
                "git@github.com:org/repo.git",
                None,
            )
            mock_service.git_amend.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "message": "Amend with identity",
            }

            handlers.git_amend(params, mock_user)

            # Check that git_amend was called with env containing identity
            call_kwargs = mock_service.git_amend.call_args[1]
            env = call_kwargs.get("env", {})
            assert env.get("GIT_AUTHOR_NAME") == "Jane Doe"
            assert env.get("GIT_AUTHOR_EMAIL") == "jane@example.com"
            assert env.get("GIT_COMMITTER_NAME") == "Jane Doe"
            assert env.get("GIT_COMMITTER_EMAIL") == "jane@example.com"

    def test_handler_git_command_error_returns_error_response(self, mock_user):
        """GitCommandError from service is caught and returned as error."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.services.git_operations_service import GitCommandError

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers._get_pat_credential_for_remote"
            ) as mock_cred,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_cred.return_value = (
                {
                    "token": "ghp_testtoken",
                    "git_user_name": "User",
                    "git_user_email": "user@x.com",
                },
                "git@github.com:org/repo.git",
                None,
            )
            mock_service.git_amend.side_effect = GitCommandError(
                "git commit --amend failed", stderr="nothing to amend"
            )

            params = {
                "repository_alias": "test-repo",
                "message": "Fix",
            }

            mcp_response = handlers.git_amend(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "error" in data

    def test_handler_cred_error_returns_error_response(self, mock_user):
        """Credential lookup error returns error response."""
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
            mock_cred.return_value = (
                None,
                None,
                "No credentials found for this repository",
            )

            params = {
                "repository_alias": "test-repo",
                "message": "Fix",
            }

            mcp_response = handlers.git_amend(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "credentials" in data["error"].lower()
                or "No credentials" in data["error"]
            )

    def test_handler_repo_not_found_returns_error(self, mock_user):
        """Handler returns error when repository cannot be resolved."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = (None, "Repository 'unknown' not found")

            params = {
                "repository_alias": "unknown",
                "message": "Fix",
            }

            mcp_response = handlers.git_amend(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "not found" in data["error"]
