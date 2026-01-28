"""
TruncationHelper for content truncation with cache storage.

Story #33: File Content Returns Cache Handle on Truncation
Implements AC6: TruncationHelper class created for reuse

This module provides a reusable helper class that encapsulates truncation logic,
cache storage, and handle generation for large content that exceeds token limits.
"""

import math
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.cache.payload_cache import PayloadCache
    from code_indexer.server.utils.config_manager import ContentLimitsConfig


@dataclass
class TruncationResult:
    """Result of truncation operation.

    Attributes:
        preview: Truncated content or full content if under limit
        cache_handle: Handle for retrieving full content if truncated, None otherwise
        truncated: Whether content was truncated
        original_tokens: Token count of original content
        preview_tokens: Token count of preview content
        total_pages: Number of pages when retrieving via get_cached_content (0 if not cached)
        has_more: Whether there's more content to retrieve (True when truncated)
    """

    preview: str
    cache_handle: Optional[str]
    truncated: bool
    original_tokens: int
    preview_tokens: int
    total_pages: int
    has_more: bool


class TruncationHelper:
    """Helper for truncating content and caching excess in PayloadCache.

    This class encapsulates the logic for:
    - Estimating token counts from character content
    - Truncating content that exceeds configured token limits
    - Storing full content in PayloadCache when truncation occurs
    - Returning appropriate cache handles for later retrieval

    Story #33: Designed for reuse across get_file_content, git_diff, git_log, etc.
    """

    def __init__(
        self,
        payload_cache: "PayloadCache",
        content_limits: "ContentLimitsConfig",
    ):
        """Initialize with cache and config dependencies.

        Args:
            payload_cache: PayloadCache instance for storing truncated content
            content_limits: ContentLimitsConfig with token limits and ratios
        """
        self.payload_cache = payload_cache
        self.content_limits = content_limits

    def estimate_tokens(self, content: str) -> int:
        """Estimate token count using chars_per_token ratio.

        Args:
            content: String content to estimate tokens for

        Returns:
            Estimated number of tokens
        """
        if not content:
            return 0
        # Floor division (//) returns int when both operands are int
        return len(content) // self.content_limits.chars_per_token

    def _get_max_tokens_for_type(self, content_type: str) -> int:
        """Get the maximum token limit for a content type.

        Args:
            content_type: Type of content ("file", "diff", "log", "search")

        Returns:
            Maximum tokens allowed for this content type
        """
        type_limits: dict[str, int] = {
            "file": int(self.content_limits.file_content_max_tokens),
            "diff": int(self.content_limits.git_diff_max_tokens),
            "log": int(self.content_limits.git_log_max_tokens),
            "search": int(self.content_limits.search_result_max_tokens),
        }
        default_limit = int(self.content_limits.file_content_max_tokens)
        return type_limits.get(content_type, default_limit)

    def truncate_and_cache(
        self,
        content: str,
        content_type: str,
    ) -> TruncationResult:
        """Truncate content if needed and cache the full content.

        Story #50: Converted from async to sync for FastAPI thread pool execution.

        Args:
            content: Full content to potentially truncate
            content_type: Type of content for limit selection ("file", "diff", "log")

        Returns:
            TruncationResult with preview, cache_handle, and token info
        """
        original_tokens = self.estimate_tokens(content)
        max_tokens = self._get_max_tokens_for_type(content_type)

        # Check if truncation is needed
        if original_tokens <= max_tokens:
            # No truncation needed - return full content
            # total_pages=0 and has_more=False since nothing is cached
            return TruncationResult(
                preview=content,
                cache_handle=None,
                truncated=False,
                original_tokens=original_tokens,
                preview_tokens=original_tokens,
                total_pages=0,
                has_more=False,
            )

        # Calculate truncation point (max_tokens * chars_per_token)
        max_chars = max_tokens * self.content_limits.chars_per_token
        preview = content[:max_chars]
        preview_tokens = self.estimate_tokens(preview)

        # Store full content in cache (sync call)
        cache_handle = self.payload_cache.store(content)

        # Calculate total_pages based on content size and max_fetch_size_chars
        # This matches the pagination calculation in PayloadCache.retrieve()
        max_fetch_size = self.payload_cache.config.max_fetch_size_chars
        total_pages = max(1, math.ceil(len(content) / max_fetch_size))

        return TruncationResult(
            preview=preview,
            cache_handle=cache_handle,
            truncated=True,
            original_tokens=original_tokens,
            preview_tokens=preview_tokens,
            total_pages=total_pages,
            has_more=True,  # Always True when truncated since there's cached content
        )
