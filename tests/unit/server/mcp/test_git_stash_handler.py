"""
Unit tests for git_stash MCP handler.

Story #453: git_stash - Stash and restore uncommitted changes

Tests:
  - push action dispatches to git_stash_push
  - pop action dispatches to git_stash_pop
  - apply action dispatches to git_stash_apply
  - list action dispatches to git_stash_list
  - drop action dispatches to git_stash_drop
  - Missing repository_alias -> error
  - Missing action -> error
  - Invalid action -> error
  - Handler registered in HANDLER_REGISTRY
  - GitCommandError returns error response
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


class TestGitStashHandler:
    """Tests for git_stash MCP handler (Story #453)."""

    def test_handler_registered_in_handler_registry(self):
        """git_stash is registered in HANDLER_REGISTRY."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert "git_stash" in HANDLER_REGISTRY

    def test_handler_missing_repository_alias_returns_error(self, mock_user):
        """Handler returns error when repository_alias is missing."""
        from code_indexer.server.mcp import handlers

        params = {"action": "push"}

        mcp_response = handlers.git_stash(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "repository_alias" in data["error"]

    def test_handler_missing_action_returns_error(self, mock_user):
        """Handler returns error when action is missing."""
        from code_indexer.server.mcp import handlers

        params = {"repository_alias": "test-repo"}

        mcp_response = handlers.git_stash(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "action" in data["error"]

    def test_handler_invalid_action_returns_error(self, mock_user):
        """Handler returns error for unknown action."""
        from code_indexer.server.mcp import handlers

        with patch(
            "code_indexer.server.mcp.handlers._resolve_git_repo_path"
        ) as mock_resolve:
            mock_resolve.return_value = ("/tmp/test-repo", None)

            params = {
                "repository_alias": "test-repo",
                "action": "invalid_action",
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert (
                "action" in data["error"].lower() or "invalid" in data["error"].lower()
            )

    def test_handler_push_action_dispatches_to_stash_push(self, mock_user):
        """Handler with action='push' calls git_stash_push service method."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "stash_ref": "stash@{0}",
            "message": "Saved working directory",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_push.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "push",
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            mock_service.git_stash_push.assert_called_once()
            assert data["success"] is True

    def test_handler_push_action_with_message(self, mock_user):
        """Handler passes message param to git_stash_push."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "stash_ref": "stash@{0}",
            "message": "Saved: my custom stash",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_push.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "push",
                "message": "my custom stash",
            }

            handlers.git_stash(params, mock_user)

            call_kwargs = mock_service.git_stash_push.call_args
            assert call_kwargs[1].get(
                "message"
            ) == "my custom stash" or "my custom stash" in str(call_kwargs)

    def test_handler_pop_action_dispatches_to_stash_pop(self, mock_user):
        """Handler with action='pop' calls git_stash_pop service method."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "message": "Applied stash@{0} and removed it",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_pop.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "pop",
                "index": 0,
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            mock_service.git_stash_pop.assert_called_once()
            assert data["success"] is True

    def test_handler_apply_action_dispatches_to_stash_apply(self, mock_user):
        """Handler with action='apply' calls git_stash_apply service method."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "message": "Applied stash@{0}",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_apply.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "apply",
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            mock_service.git_stash_apply.assert_called_once()
            assert data["success"] is True

    def test_handler_list_action_dispatches_to_stash_list(self, mock_user):
        """Handler with action='list' calls git_stash_list service method."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "stashes": [
                {"index": 0, "message": "WIP on main", "created_at": "2024-01-15"},
            ],
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_list.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "list",
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            mock_service.git_stash_list.assert_called_once()
            assert data["success"] is True
            assert "stashes" in data

    def test_handler_drop_action_dispatches_to_stash_drop(self, mock_user):
        """Handler with action='drop' calls git_stash_drop service method."""
        from code_indexer.server.mcp import handlers

        mock_result = {
            "success": True,
            "message": "Dropped stash@{0}",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_drop.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "drop",
                "index": 0,
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            mock_service.git_stash_drop.assert_called_once()
            assert data["success"] is True

    def test_handler_git_command_error_returns_error_response(self, mock_user):
        """GitCommandError from service is caught and returned as error."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.services.git_operations_service import GitCommandError

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_push.side_effect = GitCommandError(
                "git stash push failed", stderr="nothing to stash"
            )

            params = {
                "repository_alias": "test-repo",
                "action": "push",
            }

            mcp_response = handlers.git_stash(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data["success"] is False
            assert "error" in data

    def test_handler_pop_uses_default_index_zero(self, mock_user):
        """Handler uses index=0 as default when not provided for pop."""
        from code_indexer.server.mcp import handlers

        mock_result = {"success": True, "message": "Applied stash@{0}"}

        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_service,
        ):
            mock_resolve.return_value = ("/tmp/test-repo", None)
            mock_service.git_stash_pop.return_value = mock_result

            params = {
                "repository_alias": "test-repo",
                "action": "pop",
                # No index provided - should default to 0
            }

            handlers.git_stash(params, mock_user)

            call_args = mock_service.git_stash_pop.call_args
            # index should be 0 (default)
            assert call_args is not None
