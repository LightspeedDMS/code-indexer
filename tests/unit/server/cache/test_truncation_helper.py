"""
Unit tests for TruncationHelper class.

Story #33: File Content Returns Cache Handle on Truncation
Tests AC6: TruncationHelper class created for reuse
"""

import pytest
from pathlib import Path

from code_indexer.server.utils.config_manager import ContentLimitsConfig


class TestTruncationResult:
    """Test TruncationResult dataclass."""

    def test_truncation_result_dataclass_fields(self):
        """Test TruncationResult has required fields."""
        from code_indexer.server.cache.truncation_helper import TruncationResult

        result = TruncationResult(
            preview="content preview",
            cache_handle="ch_abc123",
            truncated=True,
            original_tokens=100,
            preview_tokens=50,
            total_pages=5,
            has_more=True,
        )

        assert result.preview == "content preview"
        assert result.cache_handle == "ch_abc123"
        assert result.truncated is True
        assert result.original_tokens == 100
        assert result.preview_tokens == 50
        assert result.total_pages == 5
        assert result.has_more is True

    def test_truncation_result_no_cache_handle(self):
        """Test TruncationResult with None cache_handle for non-truncated content."""
        from code_indexer.server.cache.truncation_helper import TruncationResult

        result = TruncationResult(
            preview="full content",
            cache_handle=None,
            truncated=False,
            original_tokens=10,
            preview_tokens=10,
            total_pages=0,
            has_more=False,
        )

        assert result.cache_handle is None
        assert result.truncated is False
        assert result.original_tokens == result.preview_tokens
        assert result.total_pages == 0
        assert result.has_more is False


class TestTruncationHelperBasic:
    """Test basic TruncationHelper functionality."""

    @pytest.fixture
    def content_limits(self) -> ContentLimitsConfig:
        """Create content limits config for testing."""
        return ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=100,  # 400 chars max
            cache_ttl_seconds=3600,
        )

    @pytest.fixture
    async def payload_cache(self, tmp_path: Path):
        """Create a real PayloadCache for testing."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        db_path = tmp_path / "test_cache.db"
        config = PayloadCacheConfig(
            preview_size_chars=200,
            max_fetch_size_chars=500,
            cache_ttl_seconds=3600,
        )
        cache = PayloadCache(db_path, config)
        await cache.initialize()
        return cache

    @pytest.fixture
    async def truncation_helper(self, payload_cache, content_limits):
        """Create TruncationHelper instance for testing."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        return TruncationHelper(payload_cache, content_limits)

    @pytest.mark.asyncio
    async def test_init_with_dependencies(self, payload_cache, content_limits):
        """Test TruncationHelper can be instantiated with required dependencies."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        helper = TruncationHelper(payload_cache, content_limits)
        assert helper is not None
        assert helper.payload_cache is payload_cache
        assert helper.content_limits is content_limits

    @pytest.mark.asyncio
    async def test_estimate_tokens(self, truncation_helper):
        """Test token estimation uses chars_per_token ratio."""
        # With chars_per_token=4, 100 chars = 25 tokens
        tokens = truncation_helper.estimate_tokens("a" * 100)
        assert tokens == 25

        # Empty string = 0 tokens
        tokens = truncation_helper.estimate_tokens("")
        assert tokens == 0

        # 4 chars = 1 token
        tokens = truncation_helper.estimate_tokens("abcd")
        assert tokens == 1

    @pytest.mark.asyncio
    async def test_truncate_and_cache_small_content_no_truncation(
        self, truncation_helper
    ):
        """Test that small content is not truncated and no cache handle is returned."""
        small_content = "Small content under limit"  # 25 chars = ~6 tokens

        result = await truncation_helper.truncate_and_cache(
            content=small_content,
            content_type="file",
        )

        assert result.truncated is False
        assert result.cache_handle is None
        assert result.preview == small_content
        assert result.original_tokens == truncation_helper.estimate_tokens(small_content)
        assert result.preview_tokens == result.original_tokens

    @pytest.mark.asyncio
    async def test_truncate_and_cache_large_content_is_truncated(
        self, truncation_helper, payload_cache
    ):
        """Test that large content is truncated and cache handle is returned."""
        # Content limits: 100 tokens * 4 chars/token = 400 chars max
        large_content = "x" * 1000  # 1000 chars = 250 tokens, exceeds 100 token limit

        result = await truncation_helper.truncate_and_cache(
            content=large_content,
            content_type="file",
        )

        assert result.truncated is True
        assert result.cache_handle is not None
        assert len(result.preview) == 400  # truncated to 100 tokens * 4 chars
        assert result.original_tokens == 250  # 1000 chars / 4
        assert result.preview_tokens == 100  # truncated to limit

        # Verify full content can be retrieved from cache (via pagination)
        all_content = ""
        page = 0
        while True:
            cached = await payload_cache.retrieve(result.cache_handle, page=page)
            all_content += cached.content
            if not cached.has_more:
                break
            page += 1
        assert all_content == large_content

    @pytest.mark.asyncio
    async def test_truncate_and_cache_exact_limit_not_truncated(self, truncation_helper):
        """Test content at exactly the token limit is not truncated."""
        # 100 tokens * 4 chars = 400 chars exactly at limit
        exact_content = "a" * 400

        result = await truncation_helper.truncate_and_cache(
            content=exact_content,
            content_type="file",
        )

        assert result.truncated is False
        assert result.cache_handle is None
        assert result.preview == exact_content
        assert result.original_tokens == 100
