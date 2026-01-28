"""Unit tests for PayloadCache sync conversion.

Story #50: Sync Payload Cache and Truncation Helpers
AC1: PayloadCache Sync Conversion

These tests verify that PayloadCache methods are synchronous (not coroutines).
TDD: Tests written BEFORE implementation.
"""

import inspect
import pytest
import tempfile
from pathlib import Path


class TestPayloadCacheSyncMethods:
    """Tests verifying PayloadCache methods are sync (not async)."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    def test_initialize_is_sync_method(self, temp_db_path):
        """
        PayloadCache.initialize() should be a sync method, not async.

        Given PayloadCache class
        When checking if initialize is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify initialize is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.initialize
        ), "initialize() should be sync, not async"

    def test_store_is_sync_method(self, temp_db_path):
        """
        PayloadCache.store() should be a sync method, not async.

        Given PayloadCache class
        When checking if store is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify store is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.store
        ), "store() should be sync, not async"

    def test_store_with_key_is_sync_method(self, temp_db_path):
        """
        PayloadCache.store_with_key() should be a sync method, not async.

        Given PayloadCache class
        When checking if store_with_key is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify store_with_key is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.store_with_key
        ), "store_with_key() should be sync, not async"

    def test_has_key_is_sync_method(self, temp_db_path):
        """
        PayloadCache.has_key() should be a sync method, not async.

        Given PayloadCache class
        When checking if has_key is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify has_key is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.has_key
        ), "has_key() should be sync, not async"

    def test_retrieve_is_sync_method(self, temp_db_path):
        """
        PayloadCache.retrieve() should be a sync method, not async.

        Given PayloadCache class
        When checking if retrieve is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify retrieve is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.retrieve
        ), "retrieve() should be sync, not async"

    def test_truncate_result_is_sync_method(self, temp_db_path):
        """
        PayloadCache.truncate_result() should be a sync method, not async.

        Given PayloadCache class
        When checking if truncate_result is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify truncate_result is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.truncate_result
        ), "truncate_result() should be sync, not async"

    def test_cleanup_expired_is_sync_method(self, temp_db_path):
        """
        PayloadCache.cleanup_expired() should be a sync method, not async.

        Given PayloadCache class
        When checking if cleanup_expired is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify cleanup_expired is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.cleanup_expired
        ), "cleanup_expired() should be sync, not async"

    def test_close_is_sync_method(self, temp_db_path):
        """
        PayloadCache.close() should be a sync method, not async.

        Given PayloadCache class
        When checking if close is a coroutine function
        Then it should return False (sync method)
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Verify close is NOT a coroutine function
        assert not inspect.iscoroutinefunction(
            cache.close
        ), "close() should be sync, not async"


class TestPayloadCacheSyncBehavior:
    """Tests verifying PayloadCache sync operations work correctly."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    @pytest.fixture
    def cache(self, temp_db_path):
        """Create and initialize a sync PayloadCache instance for testing."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(preview_size_chars=100)
        cache = PayloadCache(db_path=temp_db_path, config=config)
        cache.initialize()  # Sync call, no await
        yield cache
        cache.close()  # Sync call, no await

    def test_sync_store_returns_uuid4_handle(self, cache):
        """
        Sync store() should return a valid UUID4 handle.

        Given initialized sync PayloadCache
        When calling store() synchronously
        Then it should return a UUID4 handle without await
        """
        import uuid

        content = "Test content for sync caching"
        handle = cache.store(content)  # Sync call, no await

        # Verify it's a valid UUID4
        parsed_uuid = uuid.UUID(handle, version=4)
        assert str(parsed_uuid) == handle

    def test_sync_retrieve_returns_content(self, cache):
        """
        Sync retrieve() should return cached content.

        Given content stored in sync PayloadCache
        When calling retrieve() synchronously
        Then it should return the content without await
        """
        content = "Test content for sync retrieval"
        handle = cache.store(content)  # Sync call

        result = cache.retrieve(handle, page=0)  # Sync call, no await

        assert result.content == content
        assert result.page == 0

    def test_sync_truncate_result_large_content(self, cache):
        """
        Sync truncate_result() should truncate and cache large content.

        Given initialized sync PayloadCache with preview_size=100
        When calling truncate_result() with large content
        Then it should return truncation info without await
        """
        large_content = "X" * 500  # Larger than preview_size
        result = cache.truncate_result(large_content)  # Sync call, no await

        assert result["preview"] == "X" * 100
        assert result["has_more"] is True
        assert result["total_size"] == 500
        assert result["cache_handle"] is not None

    def test_sync_truncate_result_small_content(self, cache):
        """
        Sync truncate_result() should not truncate small content.

        Given initialized sync PayloadCache with preview_size=100
        When calling truncate_result() with small content
        Then it should return full content without await
        """
        small_content = "Small"  # Smaller than preview_size
        result = cache.truncate_result(small_content)  # Sync call, no await

        assert result["content"] == small_content
        assert result["has_more"] is False
        assert result["cache_handle"] is None

    def test_sync_store_with_key_and_retrieve(self, cache):
        """
        Sync store_with_key() and retrieve() should work together.

        Given initialized sync PayloadCache
        When calling store_with_key() and retrieve() synchronously
        Then content should be retrievable by explicit key
        """
        explicit_key = "test:explicit-key-123"
        content = "Content with explicit key"

        cache.store_with_key(explicit_key, content)  # Sync call, no await
        result = cache.retrieve(explicit_key, page=0)  # Sync call, no await

        assert result.content == content

    def test_sync_has_key_returns_true_for_existing(self, cache):
        """
        Sync has_key() should return True for existing keys.

        Given content stored with explicit key
        When calling has_key() synchronously
        Then it should return True without await
        """
        explicit_key = "test:has-key-check"
        cache.store_with_key(explicit_key, "Some content")  # Sync call

        result = cache.has_key(explicit_key)  # Sync call, no await

        assert result is True

    def test_sync_has_key_returns_false_for_missing(self, cache):
        """
        Sync has_key() should return False for missing keys.

        Given no content stored for a key
        When calling has_key() synchronously
        Then it should return False without await
        """
        result = cache.has_key("test:nonexistent")  # Sync call, no await

        assert result is False


class TestPayloadCacheSyncThreadSafety:
    """Tests verifying PayloadCache uses thread-safe SQLite operations."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    def test_uses_sqlite3_not_aiosqlite(self):
        """
        PayloadCache should import sqlite3, not aiosqlite.

        Given PayloadCache module
        When inspecting imports
        Then sqlite3 should be used (not aiosqlite)
        """
        import code_indexer.server.cache.payload_cache as cache_module
        import sys

        # Verify sqlite3 is imported
        assert "sqlite3" in sys.modules, "sqlite3 should be imported"

        # Check module source doesn't use aiosqlite
        import inspect

        source = inspect.getsource(cache_module)
        assert "aiosqlite" not in source, "aiosqlite should not be used in sync implementation"
        assert "sqlite3" in source, "sqlite3 should be used in sync implementation"
