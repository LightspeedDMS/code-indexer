"""
Unit tests for Bug #469 Fix: Block git_branch_switch on golden repo base clones.

Tests verify that git_branch_switch rejects -global aliases and directs
users to change_golden_repo_branch instead.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp import handlers


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
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


class TestGitBranchSwitchGoldenRepoGuard:
    """Test Bug #469 guard: git_branch_switch must reject -global aliases."""

    def test_global_alias_returns_error(self, mock_user):
        """git_branch_switch with -global alias must return an error without calling git."""
        params = {
            "repository_alias": "my-repo-global",
            "branch_name": "main",
        }

        mcp_response = handlers.git_branch_switch(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert data["success"] is False
        assert "error" in data

    def test_global_alias_error_mentions_change_golden_repo_branch(self, mock_user):
        """Error message must direct users to change_golden_repo_branch."""
        params = {
            "repository_alias": "some-repo-global",
            "branch_name": "feature-x",
        }

        mcp_response = handlers.git_branch_switch(params, mock_user)
        data = _extract_response_data(mcp_response)

        assert "change_golden_repo_branch" in data["error"]

    def test_non_global_alias_proceeds_normally(self, mock_user):
        """git_branch_switch with a non-global alias must NOT be blocked."""
        params = {
            "repository_alias": "my-regular-repo",
            "branch_name": "main",
        }

        # Mock _resolve_git_repo_path and git_operations_service so the
        # handler can proceed past the guard without real git infrastructure.
        with (
            patch(
                "code_indexer.server.mcp.handlers._resolve_git_repo_path",
                return_value=(Path("/tmp/test-repo"), None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.git_operations_service"
            ) as mock_git_svc,
        ):
            mock_git_svc.git_branch_switch.return_value = {
                "success": True,
                "current_branch": "main",
            }

            mcp_response = handlers.git_branch_switch(params, mock_user)
            data = _extract_response_data(mcp_response)

        # The guard must not have blocked this call — the git service was invoked.
        mock_git_svc.git_branch_switch.assert_called_once()
        assert data["success"] is True
