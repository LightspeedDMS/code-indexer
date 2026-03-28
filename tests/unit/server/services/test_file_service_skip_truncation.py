"""
Unit tests for FileService skip_truncation parameter.

Story #33 Fix: Double truncation issue resolution.

The problem: FileService applies truncation FIRST (max_tokens_per_request=5000 tokens),
then MCP handler applies TruncationHelper SECOND (file_content_max_tokens=50000 tokens).
Since FileService truncates first, content is already small when reaching TruncationHelper,
so cache_handle feature never triggers.

The solution: MCP handler should request FULL content from FileService (skip_truncation=True)
and then apply TruncationHelper for proper cache_handle support.

This test suite validates the skip_truncation parameter behavior.
"""

import os
import tempfile
import shutil
from pathlib import Path

from code_indexer.server.services.file_service import FileListingService
from code_indexer.server.services.config_service import (
    get_config_service,
    reset_config_service,
)


class TestFileServiceSkipTruncation:
    """Test suite for FileService skip_truncation parameter."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.temp_dir) / "test_repo"
        self.repo_path.mkdir(parents=True)

        # Create a LARGE test file that would normally be truncated by FileService
        # FileService default: 5000 tokens * 4 chars/token = 20,000 chars max
        # We need a file larger than 20,000 chars to trigger truncation
        self.large_file_path = self.repo_path / "large_file.py"
        self.large_file_lines = []
        # Create 1000 lines of ~100 chars each = ~100,000 chars total
        for i in range(1, 1001):
            line = f"# This is line {i:04d} with substantial content padding to ensure large file size generated\n"
            self.large_file_lines.append(line)

        with open(self.large_file_path, "w", encoding="utf-8") as f:
            f.writelines(self.large_file_lines)

        self.full_content = "".join(self.large_file_lines)
        self.full_content_len = len(self.full_content)

        # Create a small test file that fits within limits
        self.small_file_path = self.repo_path / "small_file.py"
        self.small_file_lines = [f"# Line {i}\n" for i in range(1, 11)]
        with open(self.small_file_path, "w", encoding="utf-8") as f:
            f.writelines(self.small_file_lines)

        # Save original environment
        self._original_env = os.environ.get("CIDX_SERVER_DATA_DIR")

        # Set environment to use temp directory for config
        self.config_dir = Path(self.temp_dir) / "cidx_config"
        self.config_dir.mkdir(parents=True)
        os.environ["CIDX_SERVER_DATA_DIR"] = str(self.config_dir)

        # Reset config service singleton to pick up new environment
        reset_config_service()

        # Initialize service
        self.service = FileListingService()

    def teardown_method(self):
        """Clean up test fixtures."""
        # Reset config service singleton
        reset_config_service()

        # Restore original environment
        if self._original_env is not None:
            os.environ["CIDX_SERVER_DATA_DIR"] = self._original_env
        else:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _get_config(self):
        """Get current file content limits config."""
        config_service = get_config_service()
        return config_service.get_config().file_content_limits_config

    def test_large_file_truncated_without_skip_truncation(self):
        """Verify that large files ARE truncated when skip_truncation=False (default)."""
        # Without skip_truncation, FileService should truncate the large file
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
            offset=None,
            limit=None,
            # skip_truncation not provided = False (default)
        )

        content = result["content"]
        metadata = result["metadata"]
        config = self._get_config()

        # Content should be truncated to max_chars_per_request
        assert len(content) <= config.max_chars_per_request
        assert metadata["truncated"] is True
        assert metadata["truncated_at_line"] is not None
        assert len(content) < self.full_content_len  # Much smaller than original

    def test_large_file_not_truncated_with_skip_truncation_true(self):
        """Verify that large files are NOT truncated when skip_truncation=True.

        This is the core fix for Story #33 double truncation issue.
        When skip_truncation=True, FileService returns FULL content so that
        TruncationHelper in the MCP handler can apply its own truncation with
        proper cache_handle support.
        """
        # With skip_truncation=True, FileService should return full content
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
            offset=None,
            limit=None,
            skip_truncation=True,
        )

        content = result["content"]
        metadata = result["metadata"]

        # Content should be FULL (not truncated)
        assert content == self.full_content
        assert len(content) == self.full_content_len

        # Metadata should indicate no truncation was applied
        assert metadata["truncated"] is False
        assert metadata["truncated_at_line"] is None

        # All lines should be returned
        assert metadata["total_lines"] == 1000
        assert metadata["returned_lines"] == 1000

    def test_skip_truncation_does_not_affect_small_files(self):
        """Verify skip_truncation has no effect on small files (already within limits)."""
        # Small file should return same content regardless of skip_truncation
        result_without = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="small_file.py",
            offset=None,
            limit=None,
            skip_truncation=False,
        )

        result_with = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="small_file.py",
            offset=None,
            limit=None,
            skip_truncation=True,
        )

        # Both should return identical content for small files
        assert result_without["content"] == result_with["content"]
        assert result_without["metadata"]["truncated"] is False
        assert result_with["metadata"]["truncated"] is False

    def test_skip_truncation_still_respects_user_limit(self):
        """Verify skip_truncation only affects token truncation, not user-specified limit.

        User-specified 'limit' parameter should still be respected even with
        skip_truncation=True, because that's a user-requested pagination boundary.
        """
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
            offset=1,
            limit=50,  # User wants only 50 lines
            skip_truncation=True,
        )

        metadata = result["metadata"]

        # User-specified limit should be respected
        assert metadata["returned_lines"] == 50
        assert metadata["offset"] == 1
        # But no token-based truncation should have occurred
        assert metadata["truncated"] is False

    def test_skip_truncation_with_offset(self):
        """Verify skip_truncation works correctly with offset parameter."""
        # Request content starting from line 500 with skip_truncation
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
            offset=500,
            limit=None,
            skip_truncation=True,
        )

        content = result["content"]
        metadata = result["metadata"]

        # Should return remaining lines from 500 to 1000 (501 lines)
        assert metadata["offset"] == 500
        assert metadata["returned_lines"] == 501  # Lines 500-1000 inclusive
        assert metadata["truncated"] is False

        # Content should start with line 500
        expected_first_line = self.large_file_lines[499]  # 0-indexed
        assert content.startswith(expected_first_line.rstrip("\n"))

    def test_skip_truncation_bypasses_line_default_limit(self):
        """Verify skip_truncation bypasses the DEFAULT_MAX_LINES limit.

        FileService has DEFAULT_MAX_LINES = 500 when limit=None.
        With skip_truncation=True, this should also be bypassed.
        """
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
            offset=None,
            limit=None,  # Would normally default to 500 lines
            skip_truncation=True,
        )

        metadata = result["metadata"]

        # Should return ALL 1000 lines, not limited to 500
        assert metadata["total_lines"] == 1000
        assert metadata["returned_lines"] == 1000

    def test_skip_truncation_default_is_false(self):
        """Verify skip_truncation defaults to False for backward compatibility."""
        # Call without skip_truncation parameter
        result = self.service.get_file_content_by_path(
            repo_path=str(self.repo_path),
            file_path="large_file.py",
        )

        # Should behave as skip_truncation=False (content truncated)
        config = self._get_config()
        assert len(result["content"]) <= config.max_chars_per_request
        # Large file should be truncated
        assert result["metadata"]["truncated"] is True


class TestGetFileContentSkipTruncation:
    """Test skip_truncation parameter for get_file_content method.

    These tests verify that get_file_content (which looks up repos by alias)
    also supports the skip_truncation parameter with the same behavior.
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.temp_dir) / "test_repo"
        self.repo_path.mkdir(parents=True)

        # Create a LARGE test file
        self.large_file_path = self.repo_path / "large_file.py"
        self.large_file_lines = []
        for i in range(1, 1001):
            line = f"# This is line {i:04d} with substantial content padding to ensure large file size generated\n"
            self.large_file_lines.append(line)

        with open(self.large_file_path, "w", encoding="utf-8") as f:
            f.writelines(self.large_file_lines)

        self.full_content = "".join(self.large_file_lines)

        # Save original environment
        self._original_env = os.environ.get("CIDX_SERVER_DATA_DIR")

        # Set environment to use temp directory for config
        self.config_dir = Path(self.temp_dir) / "cidx_config"
        self.config_dir.mkdir(parents=True)
        os.environ["CIDX_SERVER_DATA_DIR"] = str(self.config_dir)

        # Reset config service singleton
        reset_config_service()

        self.service = FileListingService()

    def teardown_method(self):
        """Clean up test fixtures."""
        reset_config_service()

        if self._original_env is not None:
            os.environ["CIDX_SERVER_DATA_DIR"] = self._original_env
        else:
            os.environ.pop("CIDX_SERVER_DATA_DIR", None)

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_get_file_content_accepts_skip_truncation_parameter(self):
        """Verify get_file_content method signature includes skip_truncation.

        Note: This test verifies the method accepts the parameter.
        Integration tests would be needed to verify full behavior with repo lookup.
        """
        # This test will fail until we add skip_truncation to get_file_content
        # We're testing the method signature here
        import inspect

        sig = inspect.signature(self.service.get_file_content)
        param_names = list(sig.parameters.keys())

        assert "skip_truncation" in param_names, (
            "get_file_content should accept skip_truncation parameter"
        )

        # Verify default value is False
        skip_truncation_param = sig.parameters["skip_truncation"]
        assert skip_truncation_param.default is False, (
            "skip_truncation should default to False"
        )
