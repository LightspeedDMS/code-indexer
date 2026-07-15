"""Unit tests for Story #1412 - MCP add_golden_repo set-path must reject
all_branches=true when the temporal_all_branches_enabled gate is off.
"""

import json
from datetime import datetime, timezone
from typing import cast
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.repos import add_golden_repo


def _make_admin() -> User:
    return User(
        username="admin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _unwrap(mcp_response: dict) -> dict:
    """Unwrap _mcp_response()'s MCP content-array envelope back to a dict."""
    return cast(dict, json.loads(mcp_response["content"][0]["text"]))


def _make_gate_config(enabled: bool):
    mock_svc = Mock()
    mock_indexing = Mock()
    mock_indexing.temporal_all_branches_enabled = enabled
    mock_server_cfg = Mock()
    mock_server_cfg.indexing_config = mock_indexing
    mock_svc.get_config.return_value = mock_server_cfg
    return mock_svc


class TestMcpAddGoldenRepoGateOffRejectsAllBranches:
    """AC2/Scenario 4: gate off + temporal_options.all_branches=true -> structured error."""

    def test_gate_off_all_branches_true_returns_error_result(self):
        mock_grm = Mock()

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.mcp.handlers.repos.get_config_service",
                return_value=_make_gate_config(False),
            ),
        ):
            result = add_golden_repo(
                {
                    "url": "git@github.com:org/repo.git",
                    "alias": "my-repo",
                    "temporal_options": {"all_branches": True},
                },
                _make_admin(),
            )

        payload = _unwrap(result)
        assert payload["success"] is False
        assert "temporal_all_branches_enabled" in payload["error"]
        mock_grm.add_golden_repo.assert_not_called()

    def test_gate_off_no_all_branches_submits_normally(self):
        """Reject boundary must not block the ordinary (no all_branches) path."""
        mock_grm = Mock()
        mock_grm.add_golden_repo.return_value = "job-789"

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.mcp.handlers.repos.get_config_service",
                return_value=_make_gate_config(False),
            ),
        ):
            result = add_golden_repo(
                {"url": "git@github.com:org/repo.git", "alias": "my-repo"},
                _make_admin(),
            )

        payload = _unwrap(result)
        assert payload["success"] is True
        mock_grm.add_golden_repo.assert_called_once()


class TestMcpAddGoldenRepoGateOnAcceptsAllBranches:
    """AC6/Scenario 6: gate on + temporal_options.all_branches=true -> accepted."""

    def test_gate_on_all_branches_true_submits_job(self):
        mock_grm = Mock()
        mock_grm.add_golden_repo.return_value = "job-999"

        with (
            patch("code_indexer.server.app.golden_repo_manager", mock_grm),
            patch(
                "code_indexer.server.mcp.handlers.repos.get_config_service",
                return_value=_make_gate_config(True),
            ),
        ):
            result = add_golden_repo(
                {
                    "url": "git@github.com:org/repo.git",
                    "alias": "my-repo",
                    "temporal_options": {"all_branches": True},
                },
                _make_admin(),
            )

        payload = _unwrap(result)
        assert payload["success"] is True
        assert payload["job_id"] == "job-999"
        mock_grm.add_golden_repo.assert_called_once()
        call_kwargs = mock_grm.add_golden_repo.call_args.kwargs
        assert call_kwargs["temporal_options"]["all_branches"] is True
