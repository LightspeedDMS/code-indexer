"""Unit tests for TruncationHelper sync conversion.

Story #50: Sync Payload Cache and Truncation Helpers
AC2: TruncationHelper Sync Conversion

These tests verify that TruncationHelper.truncate_and_cache() is synchronous (not a coroutine).
TDD: Tests written BEFORE implementation.
"""

import inspect
import pytest
import tempfile
from pathlib import Path

from code_indexer.server.utils.config_manager import ContentLimitsConfig


class TestTruncationHelperSyncMethods:
    """Tests verifying TruncationHelper methods are sync (not async)."""

    @pytest.fixture
    def content_limits(self) -> ContentLimitsConfig:
        """Create content limits config for testing."""
        return ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=100,  # 400 chars max
            cache_ttl_seconds=3600,
        )

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test_cache.db"

    @pytest.fixture
    def payload_cache(self, temp_db_path):
        """Create a real PayloadCache for testing (now sync)."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        db_path = temp_db_path
        config = PayloadCacheConfig(
            preview_size_chars=200,
            max_fetch_size_chars=500,
            cache_ttl_seconds=3600,
        )
        cache = PayloadCache(db_path, config)
        cache.initialize()  # Sync call
        yield cache
        cache.close()  # Sync call

    def test_truncate_and_cache_is_sync_method(self, payload_cache, content_limits):
        """
        TruncationHelper.truncate_and_cache() should be a sync method, not async.

        Given TruncationHelper class
        When checking if truncate_and_cache is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        helper = TruncationHelper(payload_cache, content_limits)

        # Verify truncate_and_cache is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            helper.truncate_and_cache
        ), "truncate_and_cache() should be sync, not async"


class TestTruncationHelperSyncBehavior:
    """Tests verifying TruncationHelper sync operations work correctly."""

    @pytest.fixture
    def content_limits(self) -> ContentLimitsConfig:
        """Create content limits config for testing."""
        return ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=100,  # 400 chars max
            cache_ttl_seconds=3600,
        )

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "test_cache.db"

    @pytest.fixture
    def payload_cache(self, temp_db_path):
        """Create a real PayloadCache for testing (now sync)."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        db_path = temp_db_path
        config = PayloadCacheConfig(
            preview_size_chars=200,
            max_fetch_size_chars=500,
            cache_ttl_seconds=3600,
        )
        cache = PayloadCache(db_path, config)
        cache.initialize()  # Sync call
        yield cache
        cache.close()  # Sync call

    @pytest.fixture
    def truncation_helper(self, payload_cache, content_limits):
        """Create TruncationHelper instance for testing."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        return TruncationHelper(payload_cache, content_limits)

    def test_sync_truncate_and_cache_small_content_no_truncation(
        self, truncation_helper
    ):
        """
        Sync truncate_and_cache() should not truncate small content.

        Given initialized sync TruncationHelper
        When calling truncate_and_cache() with small content
        Then it should return without truncation
        """
        small_content = "Small content under limit"  # 25 chars = ~6 tokens

        result = truncation_helper.truncate_and_cache(
            content=small_content,
            content_type="file",
        )  # Sync call, no await

        assert result.truncated is False
        assert result.cache_handle is None
        assert result.preview == small_content
        assert result.original_tokens == truncation_helper.estimate_tokens(
            small_content
        )
        assert result.preview_tokens == result.original_tokens

    def test_sync_truncate_and_cache_large_content_is_truncated(
        self, truncation_helper, payload_cache
    ):
        """
        Sync truncate_and_cache() should truncate and cache large content.

        Given initialized sync TruncationHelper
        When calling truncate_and_cache() with large content
        Then it should truncate and return cache handle
        """
        # Content limits: 100 tokens * 4 chars/token = 400 chars max
        large_content = "x" * 1000  # 1000 chars = 250 tokens, exceeds 100 token limit

        result = truncation_helper.truncate_and_cache(
            content=large_content,
            content_type="file",
        )  # Sync call, no await

        assert result.truncated is True
        assert result.cache_handle is not None
        assert len(result.preview) == 400  # truncated to 100 tokens * 4 chars
        assert result.original_tokens == 250  # 1000 chars / 4
        assert result.preview_tokens == 100  # truncated to limit

        # Verify full content can be retrieved from cache (via pagination)
        all_content = ""
        page = 0
        while True:
            cached = payload_cache.retrieve(result.cache_handle, page=page)  # Sync call
            all_content += cached.content
            if not cached.has_more:
                break
            page += 1
        assert all_content == large_content

    def test_sync_truncate_and_cache_exact_limit_not_truncated(self, truncation_helper):
        """
        Sync truncate_and_cache() should not truncate content at exact limit.

        Given initialized sync TruncationHelper
        When calling truncate_and_cache() with content exactly at token limit
        Then it should not truncate
        """
        # 100 tokens * 4 chars = 400 chars exactly at limit
        exact_content = "a" * 400

        result = truncation_helper.truncate_and_cache(
            content=exact_content,
            content_type="file",
        )  # Sync call, no await

        assert result.truncated is False
        assert result.cache_handle is None
        assert result.preview == exact_content
        assert result.original_tokens == 100
