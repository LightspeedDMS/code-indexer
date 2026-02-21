"""
Unit tests for git_diff MCP handler truncation with cache_handle support.

Story #34: Git Diff Returns Cache Handle on Truncation

When a git diff exceeds the configured `git_diff_max_tokens` limit, store full diff
in PayloadCache and return a `cache_handle` for paginated retrieval.

Follows TDD: Tests written FIRST to define expected behavior.
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
    """Create configured mock payload cache (sync after Epic #48)."""
    cache = MagicMock()
    cache.store = Mock(return_value="cache-handle-diff-123")
    cache.config = MagicMock()
    cache.config.max_fetch_size_chars = MAX_FETCH_SIZE_CHARS
    return cache


def _mock_git_diff_result(num_files=1, lines_per_hunk=10):
    """Create a mock GitDiffResult for testing."""
    from dataclasses import dataclass

    @dataclass
    class MockHunk:
        old_start: int = 1
        old_count: int = 5
        new_start: int = 1
        new_count: int = lines_per_hunk
        content: str = ""

        def __post_init__(self):
            if not self.content:
                self.content = "@@ -1,5 +1,{} @@\n".format(lines_per_hunk)
                self.content += "-old line\n" * 5
                self.content += "+new line content here\n" * lines_per_hunk

    @dataclass
    class MockFileDiff:
        path: str
        old_path: str = None
        status: str = "modified"
        insertions: int = 0
        deletions: int = 5
        hunks: list = None

        def __post_init__(self):
            if self.hunks is None:
                self.hunks = [MockHunk(new_count=lines_per_hunk)]
            self.insertions = lines_per_hunk

    @dataclass
    class MockGitDiffResult:
        from_revision: str = "abc123"
        to_revision: str = "def456"
        files: list = None
        total_insertions: int = 0
        total_deletions: int = 0
        stat_summary: str = ""

        def __post_init__(self):
            if self.files is None:
                self.files = [
                    MockFileDiff(path=f"file{i}.py", old_path=None)
                    for i in range(num_files)
                ]
            self.total_insertions = sum(f.insertions for f in self.files)
            self.total_deletions = sum(f.deletions for f in self.files)
            self.stat_summary = f"{num_files} files changed, {self.total_insertions} insertions(+), {self.total_deletions} deletions(-)"

    return MockGitDiffResult()


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
    mock_content_limits.git_diff_max_tokens = token_limit
    mock_content_limits.chars_per_token = CHARS_PER_TOKEN
    mock_config.content_limits_config = mock_content_limits
    return mock_config


class TestGitDiffTruncationWithCacheHandle:
    """Test git_diff handler truncation with cache_handle support."""

    @pytest.fixture(autouse=True)
    def mock_activated_repo(self, tmp_path):
        """Patch ActivatedRepoManager to return a path with .git dir.

        Required because _resolve_git_repo_path now validates .git existence
        for user-activated repos (non-global aliases like 'test-repo').
        """
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        with patch(
            "code_indexer.server.mcp.handlers.ActivatedRepoManager"
        ) as MockClass:
            mock_instance = MockClass.return_value
            mock_instance.get_activated_repo_path.return_value = str(repo_dir)
            yield

    def test_large_diff_returns_cache_handle(self, mock_user, mock_payload_cache):
        """Verify large diff returns cache_handle when truncated.

        Story #34 AC1: When git diff exceeds git_diff_max_tokens, store full diff
        in PayloadCache and return cache_handle.
        """
        from code_indexer.server.mcp import handlers

        # Create a diff with many files/lines that will exceed token limit
        large_diff_result = _mock_git_diff_result(num_files=50, lines_per_hunk=100)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = large_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                LOW_TOKEN_LIMIT
            )

            result = handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                    "to_revision": "def456",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            assert data["success"] is True
            assert data.get("cache_handle") == "cache-handle-diff-123"
            assert data.get("truncated") is True
            assert data.get("has_more") is True
            assert data.get("total_tokens") > 0
            assert data.get("preview_tokens") > 0
            assert data.get("total_pages") >= 1

    def test_small_diff_no_truncation(self, mock_user, mock_payload_cache):
        """Verify small diff returns no cache_handle when not truncated.

        Story #34 AC2: Small diffs that fit within token limit should not be
        truncated and should not have cache_handle.
        """
        from code_indexer.server.mcp import handlers

        # Create a small diff that won't exceed token limit
        small_diff_result = _mock_git_diff_result(num_files=1, lines_per_hunk=5)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = small_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                HIGH_TOKEN_LIMIT
            )

            result = handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                    "to_revision": "def456",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            assert data["success"] is True
            assert data.get("cache_handle") is None
            assert data.get("truncated") is False
            mock_payload_cache.store.assert_not_called()

    def test_diff_response_contains_truncation_fields(
        self, mock_user, mock_payload_cache
    ):
        """Verify all required truncation fields are in response.

        Story #34 AC3: Response must include all truncation metadata fields.
        """
        from code_indexer.server.mcp import handlers

        large_diff_result = _mock_git_diff_result(num_files=50, lines_per_hunk=100)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = large_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                LOW_TOKEN_LIMIT
            )

            result = handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Verify all required fields are present
            required_fields = [
                "success",
                "from_revision",
                "to_revision",
                "files",
                "total_insertions",
                "total_deletions",
                "stat_summary",
                "cache_handle",
                "truncated",
                "total_tokens",
                "preview_tokens",
                "total_pages",
                "has_more",
            ]

            for field in required_fields:
                assert field in data, f"Missing required field: {field}"

    def test_diff_stores_serialized_json_in_cache(self, mock_user, mock_payload_cache):
        """Verify full diff is serialized to JSON and stored in cache.

        Story #34 AC4: Full diff result should be JSON-serialized and stored.
        """
        from code_indexer.server.mcp import handlers

        large_diff_result = _mock_git_diff_result(num_files=50, lines_per_hunk=100)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = large_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                LOW_TOKEN_LIMIT
            )

            handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                },
                mock_user,
            )

            # Verify cache.store was called with a string (serialized JSON)
            mock_payload_cache.store.assert_called_once()
            stored_content = mock_payload_cache.store.call_args[0][0]

            # Verify stored content is valid JSON
            stored_data = json.loads(stored_content)
            assert "files" in stored_data
            assert "from_revision" in stored_data
            assert "to_revision" in stored_data

    def test_diff_no_cache_when_payload_cache_unavailable(self, mock_user):
        """Verify diff returns without truncation when cache is unavailable.

        Story #34 AC5: Graceful handling when payload_cache is None.
        """
        from code_indexer.server.mcp import handlers

        large_diff_result = _mock_git_diff_result(num_files=50, lines_per_hunk=100)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = large_diff_result
            mock_git_service_class.return_value = mock_git_service

            # No payload_cache available
            mock_app.app.state.payload_cache = None
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                LOW_TOKEN_LIMIT
            )

            result = handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Should still return success with all files (no truncation)
            assert data["success"] is True
            assert data.get("cache_handle") is None
            assert data.get("truncated") is False

    def test_diff_preserves_backward_compatibility(self, mock_user, mock_payload_cache):
        """Verify existing response fields are preserved.

        Story #34 AC6: Backward compatibility - existing fields must be preserved.
        """
        from code_indexer.server.mcp import handlers

        small_diff_result = _mock_git_diff_result(num_files=2, lines_per_hunk=5)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = small_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache
            mock_config_svc.return_value.get_config.return_value = _mock_config_service(
                HIGH_TOKEN_LIMIT
            )

            result = handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                    "to_revision": "def456",
                },
                mock_user,
            )

            data = _extract_response_data(result)

            # Verify backward-compatible fields
            assert data["success"] is True
            assert data["from_revision"] == "abc123"
            assert data["to_revision"] == "def456"
            assert "files" in data
            assert len(data["files"]) == 2
            assert data["total_insertions"] > 0
            assert data["total_deletions"] > 0
            assert "stat_summary" in data

            # Verify each file has expected structure
            for file in data["files"]:
                assert "path" in file
                assert "status" in file
                assert "insertions" in file
                assert "deletions" in file
                assert "hunks" in file


class TestGitDiffTruncationHelperIntegration:
    """Test git_diff handler's integration with TruncationHelper."""

    @pytest.fixture(autouse=True)
    def mock_activated_repo(self, tmp_path):
        """Patch ActivatedRepoManager to return a path with .git dir.

        Required because _resolve_git_repo_path now validates .git existence
        for user-activated repos (non-global aliases like 'test-repo').
        """
        repo_dir = tmp_path / "test-repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        with patch(
            "code_indexer.server.mcp.handlers.ActivatedRepoManager"
        ) as MockClass:
            mock_instance = MockClass.return_value
            mock_instance.get_activated_repo_path.return_value = str(repo_dir)
            yield

    def test_uses_truncation_helper_with_diff_content_type(
        self, mock_user, mock_payload_cache
    ):
        """Verify TruncationHelper is called with content_type='diff'.

        Story #34: Ensures correct token limit is applied (git_diff_max_tokens).
        """
        from code_indexer.server.mcp import handlers
        from code_indexer.server.cache.truncation_helper import TruncationResult

        large_diff_result = _mock_git_diff_result(num_files=50, lines_per_hunk=100)

        with (
            patch("code_indexer.server.mcp.handlers._get_golden_repos_dir") as mock_dir,
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
            mock_git_service.get_diff.return_value = large_diff_result
            mock_git_service_class.return_value = mock_git_service

            mock_app.app.state.payload_cache = mock_payload_cache

            config = _mock_config_service(LOW_TOKEN_LIMIT)
            mock_config_svc.return_value.get_config.return_value = config

            # Set up mock TruncationHelper
            mock_truncation_result = TruncationResult(
                preview='{"files": [], ...}',
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

            handlers.handle_git_diff(
                {
                    "repository_alias": "test-repo",
                    "from_revision": "abc123",
                },
                mock_user,
            )

            # Verify TruncationHelper was called with content_type="diff"
            mock_truncation_helper.truncate_and_cache.assert_called_once()
            call_kwargs = mock_truncation_helper.truncate_and_cache.call_args[1]
            assert call_kwargs.get("content_type") == "diff"
