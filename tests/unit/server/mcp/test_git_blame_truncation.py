"""Unit tests for git_blame handler truncation with PayloadCache support.

Bug #1008: git_blame MCP tool times out on repos with deep history.

Tests that handle_git_blame():
- Truncates large blame responses using TruncationHelper (>200 lines)
- Passes small blame responses through unmodified
- Handles BlameErrorResult (timeout) gracefully as an MCP error response
"""

import json
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

REPO_PATH = "/fake/repo/path"
LARGE_LINE_COUNT = 250
SMALL_LINE_COUNT = 10


@pytest.fixture
def mock_user():
    """Create minimal user for testing."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _make_blame_result(line_count: int):
    """Build a mock BlameResult with the given number of lines."""
    from dataclasses import dataclass
    from typing import List

    @dataclass
    class MockBlameLine:
        line_number: int
        commit_hash: str
        short_hash: str
        author_name: str
        author_email: str
        author_date: str
        original_line_number: int
        content: str

    @dataclass
    class MockBlameResult:
        path: str
        revision: str
        lines: List[MockBlameLine]
        unique_commits: int

    lines = [
        MockBlameLine(
            line_number=i,
            commit_hash="a" * 40,
            short_hash="a" * 7,
            author_name="Author",
            author_email="author@example.com",
            author_date="2024-01-01",
            original_line_number=i,
            content=f"line {i}",
        )
        for i in range(1, line_count + 1)
    ]
    return MockBlameResult(
        path="src/file.py",
        revision="HEAD",
        lines=lines,
        unique_commits=1,
    )


@contextmanager
def _blame_handler_context(
    get_blame_return_value,
    payload_cache=None,
    truncation_result=None,
):
    """Context manager that wires all patches needed by handle_git_blame tests.

    Yields a dict of mocks so callers can assert on them if needed.
    """
    with (
        patch("code_indexer.server.mcp.handlers.git_read._get_legacy") as mock_legacy,
        patch(
            "code_indexer.global_repos.git_operations.GitOperationsService"
        ) as mock_service_cls,
        patch(
            "code_indexer.server.mcp.handlers.git_read.get_config_service"
        ) as mock_cfg,
        patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app_mod,
        patch(
            "code_indexer.server.cache.truncation_helper.TruncationHelper"
        ) as mock_th_cls,
    ):
        mock_leg = MagicMock()
        mock_leg._resolve_git_repo_path.return_value = (REPO_PATH, None)
        mock_legacy.return_value = mock_leg

        mock_service = MagicMock()
        mock_service.get_blame.return_value = get_blame_return_value
        mock_service_cls.return_value = mock_service

        mock_cfg.return_value.get_config.return_value.content_limits_config = (
            MagicMock()
        )
        mock_app_mod.app.state.payload_cache = payload_cache

        if truncation_result is not None:
            mock_th = MagicMock()
            mock_th.truncate_and_cache.return_value = truncation_result
            mock_th_cls.return_value = mock_th

        yield {
            "legacy": mock_leg,
            "service": mock_service,
            "truncation_helper_cls": mock_th_cls,
        }


class TestGitBlameTruncation:
    """Tests for blame response truncation in handle_git_blame."""

    def test_large_blame_response_is_truncated(self, mock_user):
        """Bug #1080 Finding #3: byte-envelope retired for git_blame.

        Large blame is returned in full; truncated=False, cache_handle=None.
        _apply_blame_truncation removed; _BLAME_TRUNC_ZERO used unconditionally.
        """
        blame_result = _make_blame_result(LARGE_LINE_COUNT)

        args = {"repository_alias": "my-repo", "path": "src/file.py"}

        with _blame_handler_context(
            get_blame_return_value=blame_result,
            payload_cache=None,
        ):
            from code_indexer.server.mcp.handlers.git_read import handle_git_blame

            response = handle_git_blame(args, mock_user)

        result = json.loads(response["content"][0]["text"])
        assert result["success"] is True
        assert result["truncated"] is False
        assert result["cache_handle"] is None
        assert result["total_lines"] == LARGE_LINE_COUNT

    def test_small_blame_response_passes_through(self, mock_user):
        """When blame has few lines, no truncation is applied and lines are returned directly."""
        blame_result = _make_blame_result(SMALL_LINE_COUNT)

        args = {"repository_alias": "my-repo", "path": "src/file.py"}

        with _blame_handler_context(
            get_blame_return_value=blame_result,
            payload_cache=None,  # no cache → no truncation
        ):
            from code_indexer.server.mcp.handlers.git_read import handle_git_blame

            response = handle_git_blame(args, mock_user)

        result = json.loads(response["content"][0]["text"])
        assert result["success"] is True
        assert result["truncated"] is False
        assert result["cache_handle"] is None
        assert result["total_lines"] == SMALL_LINE_COUNT
        assert len(result["lines"]) == SMALL_LINE_COUNT

    def test_blame_timeout_error_handled_in_handler(self, mock_user):
        """When get_blame() returns BlameErrorResult (timeout), handler returns MCP error."""
        from code_indexer.global_repos.git_operations import BlameErrorResult

        error_result = BlameErrorResult(
            success=False, error="Git blame timed out after 30 seconds"
        )

        args = {"repository_alias": "my-repo", "path": "src/file.py"}

        with _blame_handler_context(
            get_blame_return_value=error_result,
            payload_cache=None,
        ):
            from code_indexer.server.mcp.handlers.git_read import handle_git_blame

            response = handle_git_blame(args, mock_user)

        result = json.loads(response["content"][0]["text"])
        assert result["success"] is False
        assert "timed out" in result["error"]
