"""
Unit tests for TruncationHelper pagination info (total_pages, has_more).

Story #34: Add missing pagination information in TruncationHelper and get_file_content response.
Story #50: Updated to sync operations for FastAPI thread pool execution.

Tests:
- TruncationResult has total_pages and has_more fields
- TruncationHelper.truncate_and_cache returns correct pagination info
- Pagination calculation is based on PayloadCacheConfig.max_fetch_size_chars
"""

import pytest
from pathlib import Path

from code_indexer.server.utils.config_manager import ContentLimitsConfig


class TestTruncationResultPaginationFields:
    """Test TruncationResult dataclass has pagination fields."""

    def test_truncation_result_has_total_pages_field(self):
        """TruncationResult must have total_pages field."""
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

        assert result.total_pages == 5

    def test_truncation_result_has_has_more_field(self):
        """TruncationResult must have has_more field."""
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

        assert result.has_more is True

    def test_truncation_result_non_truncated_pagination_fields(self):
        """Non-truncated content should have total_pages=0 and has_more=False."""
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

        assert result.total_pages == 0
        assert result.has_more is False


class TestTruncationHelperPaginationCalculation:
    """Test TruncationHelper correctly calculates pagination info.

    Story #50: PayloadCache and TruncationHelper are now sync.
    """

    @pytest.fixture
    def content_limits(self) -> ContentLimitsConfig:
        """Create content limits config for testing.

        Using specific values to test pagination calculation:
        - chars_per_token=4
        - file_content_max_tokens=100 (400 chars max for preview)
        """
        return ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=100,  # 400 chars max preview
            cache_ttl_seconds=3600,
        )

    @pytest.fixture
    def payload_cache(self, tmp_path: Path):
        """Create a real PayloadCache with specific max_fetch_size_chars.

        Story #50: PayloadCache is now sync.

        max_fetch_size_chars=500 means:
        - 1000 chars content = 2 pages
        - 2500 chars content = 5 pages
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        db_path = tmp_path / "test_cache.db"
        config = PayloadCacheConfig(
            preview_size_chars=200,
            max_fetch_size_chars=500,  # 500 chars per page
            cache_ttl_seconds=3600,
        )
        cache = PayloadCache(db_path, config)
        cache.initialize()  # Sync call
        return cache

    @pytest.fixture
    def truncation_helper(self, payload_cache, content_limits):
        """Create TruncationHelper instance for testing."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        return TruncationHelper(payload_cache, content_limits)

    def test_truncated_content_returns_correct_total_pages(
        self, truncation_helper
    ):
        """Truncated content should return correct total_pages based on content size."""
        # 1000 chars content with max_fetch_size_chars=500 = 2 pages
        large_content = "x" * 1000

        result = truncation_helper.truncate_and_cache(  # Sync call
            content=large_content,
            content_type="file",
        )

        assert result.truncated is True
        assert result.total_pages == 2

    def test_truncated_content_has_more_is_true(self, truncation_helper):
        """Truncated content should have has_more=True since there's cached content."""
        large_content = "x" * 1000

        result = truncation_helper.truncate_and_cache(  # Sync call
            content=large_content,
            content_type="file",
        )

        assert result.truncated is True
        assert result.has_more is True

    def test_small_content_total_pages_zero(self, truncation_helper):
        """Non-truncated content should have total_pages=0 (no cache)."""
        small_content = "Small content"  # 13 chars = ~3 tokens, under 100 token limit

        result = truncation_helper.truncate_and_cache(  # Sync call
            content=small_content,
            content_type="file",
        )

        assert result.truncated is False
        assert result.total_pages == 0

    def test_small_content_has_more_is_false(self, truncation_helper):
        """Non-truncated content should have has_more=False."""
        small_content = "Small content"

        result = truncation_helper.truncate_and_cache(  # Sync call
            content=small_content,
            content_type="file",
        )

        assert result.truncated is False
        assert result.has_more is False

    def test_total_pages_calculation_various_sizes(self, truncation_helper):
        """Verify total_pages calculation for various content sizes.

        With max_fetch_size_chars=500:
        - 500 chars = 1 page
        - 501 chars = 2 pages
        - 1000 chars = 2 pages
        - 1500 chars = 3 pages
        - 2500 chars = 5 pages
        """
        test_cases = [
            (500, 1),   # Exactly one page
            (501, 2),   # Just over one page
            (1000, 2),  # Exactly two pages
            (1500, 3),  # Three pages
            (2500, 5),  # Five pages
        ]

        for content_size, expected_pages in test_cases:
            content = "x" * content_size

            result = truncation_helper.truncate_and_cache(  # Sync call
                content=content,
                content_type="file",
            )

            assert result.truncated is True, f"Content size {content_size} should be truncated"
            assert result.total_pages == expected_pages, (
                f"Content size {content_size} should have {expected_pages} pages, "
                f"got {result.total_pages}"
            )

    def test_total_pages_matches_cache_retrieve_pagination(
        self, truncation_helper, payload_cache
    ):
        """Verify total_pages in TruncationResult matches what PayloadCache.retrieve returns."""
        content = "x" * 1500  # 1500 chars = 3 pages with max_fetch_size_chars=500

        result = truncation_helper.truncate_and_cache(  # Sync call
            content=content,
            content_type="file",
        )

        # Retrieve from cache and verify pagination matches (sync call)
        cache_result = payload_cache.retrieve(result.cache_handle, page=0)

        assert result.total_pages == cache_result.total_pages, (
            f"TruncationResult.total_pages ({result.total_pages}) should match "
            f"CacheRetrievalResult.total_pages ({cache_result.total_pages})"
        )
