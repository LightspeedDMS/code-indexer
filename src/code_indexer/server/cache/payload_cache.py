"""Payload Cache for semantic search result truncation.

Story #679: S1 - Semantic Search with Payload Control (Foundation)
Story #50: Converted from async to sync for FastAPI thread pool execution.

Provides SQLite-based caching for large content with TTL-based eviction.
"""

import logging
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import CacheConfig
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


class CacheNotFoundError(Exception):
    """Raised when a cache handle is not found or expired."""

    pass


@dataclass
class CacheRetrievalResult:
    """Result of retrieving cached content with pagination info."""

    content: str
    page: int
    total_pages: int
    has_more: bool


@dataclass
class PayloadCacheConfig:
    """Configuration for payload cache (AC1).

    Attributes:
        preview_size_chars: Number of characters to include in preview (default 2000)
        max_fetch_size_chars: Maximum chars per page when fetching (default 5000)
        cache_ttl_seconds: Time-to-live for cache entries in seconds (default 900)
        cleanup_interval_seconds: Interval between cleanup runs in seconds (default 60)

    Story #32: Environment variable configuration has been removed.
    All configuration MUST come from the Web UI configuration system (ServerConfig).
    """

    preview_size_chars: int = 2000
    max_fetch_size_chars: int = 5000
    cache_ttl_seconds: int = 900
    cleanup_interval_seconds: int = 60

    @classmethod
    def from_server_config(
        cls, cache_config: Optional["CacheConfig"]
    ) -> "PayloadCacheConfig":
        """Create config from server CacheConfig.

        Story #32: Environment variable overrides have been removed.
        All configuration comes from Web UI/ServerConfig only.

        Args:
            cache_config: CacheConfig from server configuration (can be None)

        Returns:
            PayloadCacheConfig with values from server config or defaults
        """
        # Use server config values or defaults
        if cache_config is not None:
            return cls(
                preview_size_chars=cache_config.payload_preview_size_chars,
                max_fetch_size_chars=cache_config.payload_max_fetch_size_chars,
                cache_ttl_seconds=cache_config.payload_cache_ttl_seconds,
                cleanup_interval_seconds=cache_config.payload_cleanup_interval_seconds,
            )
        else:
            return cls()  # Use class defaults


class PayloadCache:
    """SQLite-based cache for storing large content with pagination support (AC2).

    Uses WAL mode for concurrent read/write access and stores content
    with UUID4 handles for later retrieval.
    """

    def __init__(self, db_path: Path, config: PayloadCacheConfig):
        """Initialize PayloadCache.

        Args:
            db_path: Path to SQLite database file
            config: Cache configuration
        """
        self.db_path = db_path
        self.config = config
        self._cleanup_thread: Optional[threading.Thread] = None
        self._stop_cleanup = threading.Event()

    def initialize(self) -> None:
        """Initialize database with WAL mode and create schema.

        Story #50: Converted from async to sync for FastAPI thread pool execution.
        Uses connection-per-operation pattern for thread safety.
        """
        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            # Enable WAL mode for concurrent access
            conn.execute("PRAGMA journal_mode=WAL")

            # Create table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payload_cache (
                    handle TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    total_size INTEGER NOT NULL
                )
                """
            )

            # Create index for TTL cleanup
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_payload_cache_created_at
                ON payload_cache(created_at)
                """
            )

            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """Close the cache and cleanup resources.

        Story #50: Converted from async to sync.
        """
        self.stop_background_cleanup()

    def start_background_cleanup(self) -> None:
        """Start background cleanup thread as daemon."""
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return  # Already running

        self._stop_cleanup.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="PayloadCacheCleanup",
        )
        self._cleanup_thread.start()

    def stop_background_cleanup(self) -> None:
        """Stop background cleanup thread."""
        self._stop_cleanup.set()
        if self._cleanup_thread is not None:
            self._cleanup_thread.join(timeout=2.0)

    def _cleanup_loop(self) -> None:
        """Background cleanup loop running in separate thread.

        Story #50: Simplified since cleanup_expired() is now sync.
        """
        while not self._stop_cleanup.wait(self.config.cleanup_interval_seconds):
            try:
                self.cleanup_expired()
            except Exception as e:
                logger.warning(
                    format_error_log("GIT-GENERAL-014", f"Cleanup failed: {e}")
                )

    def store(self, content: str) -> str:
        """Store content and return a UUID4 handle.

        Story #50: Converted from async to sync for FastAPI thread pool execution.
        Uses connection-per-operation pattern for thread safety.

        Args:
            content: Content to cache

        Returns:
            UUID4 handle for retrieving the content
        """
        handle = str(uuid.uuid4())
        created_at = time.time()
        total_size = len(content)

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            conn.execute(
                """
                INSERT INTO payload_cache (handle, content, created_at, total_size)
                VALUES (?, ?, ?, ?)
                """,
                (handle, content, created_at, total_size),
            )
            conn.commit()
        finally:
            conn.close()

        return handle

    def store_with_key(self, key: str, content: str) -> None:
        """Store content with explicit key.

        Story #720: Delegation Result Caching
        Story #50: Converted from async to sync for FastAPI thread pool execution.

        Unlike store() which generates UUID4, this uses the provided key.
        If key already exists, updates the content and timestamp.

        Args:
            key: Explicit key for storage (e.g., "delegation:job-uuid")
            content: Content to cache
        """
        created_at = time.time()
        total_size = len(content)

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            # Use INSERT OR REPLACE to handle updates
            conn.execute(
                """
                INSERT OR REPLACE INTO payload_cache (handle, content, created_at, total_size)
                VALUES (?, ?, ?, ?)
                """,
                (key, content, created_at, total_size),
            )
            conn.commit()
        finally:
            conn.close()

    def has_key(self, key: str) -> bool:
        """Check if a key exists in the cache without retrieving content.

        Story #720: Delegation Result Caching
        Story #50: Converted from async to sync for FastAPI thread pool execution.

        Efficiently checks existence using COUNT(*) without loading content.

        Args:
            key: The key to check (can be UUID4 from store() or explicit key)

        Returns:
            True if key exists in cache, False otherwise
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM payload_cache WHERE handle = ?",
                (key,),
            )
            row = cursor.fetchone()
            return row[0] > 0 if row else False
        finally:
            conn.close()

    def retrieve(self, handle: str, page: int = 0) -> CacheRetrievalResult:
        """Retrieve cached content by handle with pagination.

        Story #50: Converted from async to sync for FastAPI thread pool execution.

        Args:
            handle: UUID4 handle from store()
            page: Page number (0-indexed)

        Returns:
            CacheRetrievalResult with content and pagination info

        Raises:
            CacheNotFoundError: If handle not found or page out of range
        """
        if page < 0:
            raise CacheNotFoundError(f"Invalid page number: {page}")

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            cursor = conn.execute(
                "SELECT content, total_size FROM payload_cache WHERE handle = ?",
                (handle,),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        if row is None:
            raise CacheNotFoundError(f"Cache handle not found: {handle}")

        content = row[0]
        total_size = row[1]
        page_size = self.config.max_fetch_size_chars

        # Calculate pagination
        total_pages = max(1, math.ceil(total_size / page_size))

        if page >= total_pages:
            raise CacheNotFoundError(
                f"Page {page} out of range for handle {handle} (total: {total_pages})"
            )

        # Extract page content
        start = page * page_size
        end = start + page_size
        page_content = content[start:end]

        has_more = page < total_pages - 1

        return CacheRetrievalResult(
            content=page_content,
            page=page,
            total_pages=total_pages,
            has_more=has_more,
        )

    def truncate_result(self, content: str) -> dict:
        """Truncate content for semantic search response (AC3).

        Story #50: Converted from async to sync for FastAPI thread pool execution.

        For content larger than preview_size_chars:
            Returns preview, cache_handle, has_more=True, total_size

        For content <= preview_size_chars:
            Returns full content, cache_handle=None, has_more=False

        Args:
            content: Full content to potentially truncate

        Returns:
            Dict with appropriate keys based on content size
        """
        preview_size = self.config.preview_size_chars

        if len(content) > preview_size:
            # Large content: store full content and return preview
            cache_handle = self.store(content)
            return {
                "preview": content[:preview_size],
                "cache_handle": cache_handle,
                "has_more": True,
                "total_size": len(content),
            }
        else:
            # Small content: return full content without caching
            return {
                "content": content,
                "cache_handle": None,
                "has_more": False,
            }

    def cleanup_expired(self) -> int:
        """Delete cache entries older than cache_ttl_seconds.

        Story #50: Converted from async to sync for FastAPI thread pool execution.

        Returns:
            Number of entries deleted
        """
        cutoff_time = time.time() - self.config.cache_ttl_seconds

        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM payload_cache WHERE created_at < ?",
                (cutoff_time,),
            )
            row = cursor.fetchone()
            count = row[0] if row else 0

            conn.execute(
                "DELETE FROM payload_cache WHERE created_at < ?",
                (cutoff_time,),
            )
            conn.commit()
        finally:
            conn.close()

        return count
