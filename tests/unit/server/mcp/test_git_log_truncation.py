"""
Unit tests for git_log MCP handler truncation with cache_handle support.

Story #35: Git Log Returns Cache Handle on Truncation

When a git log exceeds the configured `git_log_max_tokens` limit, store full log
in PayloadCache and return a `cache_handle` for paginated retrieval.

Follows TDD: Tests written FIRST to define expected behavior.

IMPORTANT: The existing `result.truncated` from git's --limit flag is COUNT-based truncation.
The new `truncated` field reflects TOKEN-based truncation. These are distinct concepts:
- COUNT-based: Git returned fewer commits than exist (due to --limit)
- TOKEN-based: Response content exceeds configured token limit
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock, Mock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# Test configuration constants
LOW_TOKEN_LIMIT = 100  # Triggers truncation (~400 chars max)
HIGH_TOKEN_LIMIT = 50000  # No truncation
CHARS_PER_TOKEN = 4
MAX_FETCH_SIZE_CHARS = 50000


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_payload_cache():
    """Create configured mock payload cache.

    Epic #48: Handlers are now sync, so cache must use Mock (not AsyncMock).
    """
    cache = MagicMock()
    cache.store = Mock(return_value="cache-handle-log-123")
    cache.config = MagicMock()
    cache.config.max_fetch_size_chars = MAX_FETCH_SIZE_CHARS
    return cache


def _mock_git_log_result(num_commits=5, body_lines=10):
    """Create a mock GitLogResult for testing.

    Args:
        num_commits: Number of commits to generate
        body_lines: Number of lines in each commit body
    """
    from dataclasses import dataclass

    @dataclass
    class MockCommit:
        hash: str
        short_hash: str
        author_name: str
        author_email: str
        author_date: str
        committer_name: str
        committer_email: str
        committer_date: str
        subject: str
        body: str

    @dataclass
    class MockGitLogResult:
        commits: list
        total_count: int
        truncated: bool  # COUNT-based truncation from git --limit

        def __post_init__(self):
            if not self.commits:
                self.commits = []

    commits = []
    for i in range(num_commits):
        body_content = "\n".join([f"Line {j} of commit {i}" for j in range(body_lines)])
        commits.append(
            MockCommit(
                hash=f"abc{i:04d}def{i:04d}0123456789012345678901234567{i:04d}",
                short_hash=f"abc{i:04d}",
                author_name=f"Author {i}",
                author_email=f"author{i}@example.com",
                author_date=f"2024-01-{i+1:02d}T10:00:00+00:00",
                committer_name=f"Committer {i}",
                committer_email=f"committer{i}@example.com",
                committer_date=f"2024-01-{i+1:02d}T11:00:00+00:00",
                subject=f"Commit {i}: This is a test commit message with some content",
                body=body_content,
            )
        )

    return MockGitLogResult(
        commits=commits,
        total_count=num_commits,
        truncated=False,  # COUNT-based truncation (not exceeding --limit)
    )


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    if "content" in mcp_response and len(mcp_response["content"]) > 0:
        content = mcp_response["content"][0]
        if "text" in content:
            try:
                return cast(dict, json.loads(content["text"]))
            except json.JSONDecodeError:
                return {"text": content["text"]}
    return mcp_response


def _mock_config_service(token_limit):
    """Create a mock config service with specific token limit."""
    mock_config = MagicMock()
    mock_content_limits = MagicMock()
    mock_content_limits.git_log_max_tokens = token_limit
    mock_content_limits.chars_per_token = CHARS_PER_TOKEN
    mock_config.content_limits_config = mock_content_limits
    return mock_config


class TestGitLogTruncationWithCacheHandle:
    """Test git_log handler truncation with cache_handle support."""

    def test_large_log_returns_cache_handle(
        self, mock_user, mock_payload_cache
    ):
        """Verify large log returns cache_handle when truncated.

        Story #35 AC1: When git log exceeds git_log_max_tokens, store full log
        in PayloadCache and return cache_handle.
        """
        from code_indexer.server.mcp import handlers

        # Create a log with many commits that will exceed token limit
        large_log_result = _mock_git_log_result(num_commits=100, body_lines=50)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = large_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(LOW_TOKEN_LIMIT)
            )

            result = handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                    "limit": 100,
                },
                mock_user,
            )

            data = _extract_response_data(result)

            assert data["success"] is True
            assert data.get("cache_handle") == "cache-handle-log-123"
            assert data.get("truncated") is True  # TOKEN-based truncation
            assert data.get("has_more") is True
            assert data.get("total_tokens") > 0
            assert data.get("preview_tokens") > 0
            assert data.get("total_pages") >= 1

    def test_small_log_no_truncation(self, mock_user, mock_payload_cache):
        """Verify small log returns no cache_handle when not truncated.

        Story #35 AC2: Small logs that fit within token limit should not be
        truncated and should not have cache_handle.
        """
        from code_indexer.server.mcp import handlers

        # Create a small log that won't exceed token limit
        small_log_result = _mock_git_log_result(num_commits=3, body_lines=2)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = small_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(HIGH_TOKEN_LIMIT)
            )

            result = handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                    "limit": 50,
                },
                mock_user,
            )

            data = _extract_response_data(result)

            assert data["success"] is True
            assert data.get("cache_handle") is None
            assert data.get("truncated") is False  # TOKEN-based, not truncated
            mock_payload_cache.store.assert_not_called()

    def test_log_response_contains_truncation_fields(
        self, mock_user, mock_payload_cache
    ):
        """Verify all required truncation fields are in response.

        Story #35 AC3: Response must include all truncation metadata fields.
        """
        from code_indexer.server.mcp import handlers

        large_log_result = _mock_git_log_result(num_commits=100, body_lines=50)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = large_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(LOW_TOKEN_LIMIT)
            )

            result = handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Verify all required fields are present
            required_fields = [
                "success",
                "commits",
                "total_count",
                "cache_handle",
                "truncated",
                "total_tokens",
                "preview_tokens",
                "total_pages",
                "has_more",
            ]

            for field in required_fields:
                assert field in data, f"Missing required field: {field}"

    def test_log_stores_serialized_json_in_cache(
        self, mock_user, mock_payload_cache
    ):
        """Verify full log is serialized to JSON and stored in cache.

        Story #35 AC4: Full log result should be JSON-serialized and stored.
        """
        from code_indexer.server.mcp import handlers

        large_log_result = _mock_git_log_result(num_commits=100, body_lines=50)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = large_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(LOW_TOKEN_LIMIT)
            )

            handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                },
                mock_user,
            )

            # Verify cache.store was called with a string (serialized JSON)
            mock_payload_cache.store.assert_called_once()
            stored_content = mock_payload_cache.store.call_args[0][0]

            # Verify stored content is valid JSON
            stored_data = json.loads(stored_content)
            assert "commits" in stored_data
            assert "total_count" in stored_data

    def test_log_no_cache_when_payload_cache_unavailable(self, mock_user):
        """Verify log returns without truncation when cache is unavailable.

        Story #35 AC5: Graceful handling when payload_cache is None.
        """
        from code_indexer.server.mcp import handlers

        large_log_result = _mock_git_log_result(num_commits=100, body_lines=50)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = large_log_result
            mock_git_service_class.return_value = mock_git_service

            # No payload_cache available
            mock_app.app.state.payload_cache = None
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(LOW_TOKEN_LIMIT)
            )

            result = handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Should still return success with all commits (no truncation)
            assert data["success"] is True
            assert data.get("cache_handle") is None
            assert data.get("truncated") is False

    def test_log_preserves_backward_compatibility(
        self, mock_user, mock_payload_cache
    ):
        """Verify existing response fields are preserved.

        Story #35 AC6: Backward compatibility - existing fields must be preserved.
        """
        from code_indexer.server.mcp import handlers

        small_log_result = _mock_git_log_result(num_commits=5, body_lines=3)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = small_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = (
                _mock_config_service(HIGH_TOKEN_LIMIT)
            )

            result = handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                    "limit": 50,
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Verify backward-compatible fields
            assert data["success"] is True
            assert "commits" in data
            assert len(data["commits"]) == 5
            assert "total_count" in data
            assert data["total_count"] == 5

            # Verify each commit has expected structure
            for commit in data["commits"]:
                assert "hash" in commit
                assert "short_hash" in commit
                assert "author_name" in commit
                assert "author_email" in commit
                assert "author_date" in commit
                assert "committer_name" in commit
                assert "committer_email" in commit
                assert "committer_date" in commit
                assert "subject" in commit
                assert "body" in commit


class TestGitLogTruncationHelperIntegration:
    """Test git_log handler's integration with TruncationHelper."""

    def test_uses_truncation_helper_with_log_content_type(
        self, mock_user, mock_payload_cache
    ):
        """Verify TruncationHelper is called with content_type='log'.

        Story #35: Ensures correct token limit is applied (git_log_max_tokens).
        """
        from code_indexer.server.mcp import handlers
        from code_indexer.server.cache.truncation_helper import TruncationResult

        large_log_result = _mock_git_log_result(num_commits=100, body_lines=50)

        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
            patch(
                "code_indexer.server.mcp.handlers._resolve_repo_path"
            ) as mock_resolve,
            patch(
                "code_indexer.global_repos.git_operations.GitOperationsService"
            ) as mock_git_service_class,
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc,
            patch(
                "code_indexer.server.cache.truncation_helper.TruncationHelper"
            ) as mock_truncation_helper_class,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_resolve.return_value = "/fake/repo/path"

            mock_git_service = MagicMock()
            mock_git_service.get_log.return_value = large_log_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache

            config = _mock_config_service(LOW_TOKEN_LIMIT)
            mock_config_svc.return_value.get_config.return_value = config

            # Set up mock TruncationHelper (Epic #48: sync, not async)
            mock_truncation_result = TruncationResult(
                preview='{"commits": [], ...}',
                cache_handle="cache-handle-xyz",
                truncated=True,
                original_tokens=5000,
                preview_tokens=100,
                total_pages=5,
                has_more=True,
            )
            mock_truncation_helper = MagicMock()
            mock_truncation_helper.truncate_and_cache = Mock(
                return_value=mock_truncation_result
            )
            mock_truncation_helper_class.return_value = mock_truncation_helper

            handlers.handle_git_log(
                {
                    "repository_alias": "test-repo",
                },
                mock_user,
            )

            # Verify TruncationHelper was called with content_type="log"
            mock_truncation_helper.truncate_and_cache.assert_called_once()
            call_kwargs = mock_truncation_helper.truncate_and_cache.call_args[1]
            assert call_kwargs.get("content_type") == "log"
