"""Unit tests for PayloadCache cleanup operations.

Story #679: S1 - Semantic Search with Payload Control (Foundation)
Story #50: Updated to sync operations for FastAPI thread pool execution.
AC6: Background Cleanup Thread

These tests follow TDD methodology - written BEFORE implementation.
"""

import pytest
import tempfile
import time
from pathlib import Path


class TestPayloadCacheCleanup:
    """Tests for PayloadCache cleanup operations (AC6)."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    def test_cleanup_expired_removes_old_entries(self, temp_db_path):
        """Test that cleanup_expired() removes entries older than TTL."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
            CacheNotFoundError,
        )

        # Use very short TTL for testing
        config = PayloadCacheConfig(cache_ttl_seconds=1)
        cache = PayloadCache(db_path=temp_db_path, config=config)
        cache.initialize()

        # Store content
        handle = cache.store("Test content")

        # Verify content exists
        result = cache.retrieve(handle, page=0)
        assert result.content == "Test content"

        # Wait for TTL to expire
        time.sleep(1.5)

        # Run cleanup
        deleted_count = cache.cleanup_expired()
        assert deleted_count == 1

        # Verify content is gone
        with pytest.raises(CacheNotFoundError):
            cache.retrieve(handle, page=0)

        cache.close()

    def test_cleanup_expired_keeps_fresh_entries(self, temp_db_path):
        """Test that cleanup_expired() keeps entries within TTL."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(cache_ttl_seconds=300)  # 5 minutes
        cache = PayloadCache(db_path=temp_db_path, config=config)
        cache.initialize()

        handle = cache.store("Fresh content")

        # Run cleanup immediately (entry is fresh)
        deleted_count = cache.cleanup_expired()
        assert deleted_count == 0

        # Verify content still exists
        result = cache.retrieve(handle, page=0)
        assert result.content == "Fresh content"

        cache.close()


class TestPayloadCacheBackgroundCleanup:
    """Tests for background cleanup thread (AC6)."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    def test_background_cleanup_thread_starts_as_daemon(self, temp_db_path):
        """Test that background cleanup thread is started as daemon."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(cleanup_interval_seconds=1)
        cache = PayloadCache(db_path=temp_db_path, config=config)
        cache.initialize()

        # Start background cleanup
        cache.start_background_cleanup()

        assert cache._cleanup_thread is not None
        assert cache._cleanup_thread.daemon is True
        assert cache._cleanup_thread.is_alive()

        # Stop cleanup
        cache.stop_background_cleanup()
        cache.close()

    def test_stop_background_cleanup_stops_thread(self, temp_db_path):
        """Test that stop_background_cleanup() stops the thread."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(cleanup_interval_seconds=10)
        cache = PayloadCache(db_path=temp_db_path, config=config)
        cache.initialize()

        cache.start_background_cleanup()
        assert cache._cleanup_thread.is_alive()

        cache.stop_background_cleanup()
        # Give thread time to stop
        time.sleep(0.5)

        # Thread should no longer be alive
        assert not cache._cleanup_thread.is_alive()

        cache.close()


class TestPayloadCacheInitializationGate:
    """Tests for initialization gate preventing race condition (Bug #178)."""

    @pytest.fixture
    def temp_db_path(self):
        """Create a temporary database path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "payload_cache.db"

    def test_cleanup_thread_waits_for_initialization(self, temp_db_path):
        """Test that cleanup thread waits for initialize() before running cleanup."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        # Use very short interval to trigger cleanup quickly
        config = PayloadCacheConfig(cleanup_interval_seconds=0.1)
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Start cleanup BEFORE initialization
        cache.start_background_cleanup()

        # Give cleanup thread time to potentially try running (it should wait)
        time.sleep(0.2)

        # Thread should be alive but waiting
        assert cache._cleanup_thread is not None
        assert cache._cleanup_thread.is_alive()

        # Now initialize - this should unblock the cleanup thread
        cache.initialize()

        # Give cleanup thread time to run its first iteration
        time.sleep(0.3)

        # Thread should still be alive and have successfully run cleanup
        # (not crashed with "no such table" error)
        assert cache._cleanup_thread.is_alive()

        cache.stop_background_cleanup()
        cache.close()

    def test_cleanup_runs_normally_after_initialization(self, temp_db_path):
        """Test that cleanup runs normally after initialization completes."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(
            cache_ttl_seconds=1,
            cleanup_interval_seconds=0.5
        )
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Initialize first, then start cleanup (normal order)
        cache.initialize()

        # Store expired content
        handle = cache.store("Old content")
        time.sleep(1.2)  # Let it expire

        # Start background cleanup
        cache.start_background_cleanup()

        # Give cleanup thread time to run
        time.sleep(0.8)

        # Verify cleanup actually ran (expired entry should be gone)
        from code_indexer.server.cache.payload_cache import CacheNotFoundError
        with pytest.raises(CacheNotFoundError):
            cache.retrieve(handle, page=0)

        cache.stop_background_cleanup()
        cache.close()

    def test_stop_before_initialization_exits_cleanly(self, temp_db_path):
        """Test that stop works even if called before initialization."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(cleanup_interval_seconds=10)
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Start cleanup thread (it will wait for initialization)
        cache.start_background_cleanup()

        # Stop BEFORE initialization - thread should exit cleanly
        cache.stop_background_cleanup()

        # Give thread time to stop
        time.sleep(0.5)

        # Thread should be stopped (not stuck waiting forever)
        assert not cache._cleanup_thread.is_alive()

        cache.close()

    def test_initialization_event_is_set_after_initialize(self, temp_db_path):
        """Test that _initialized event is set after initialize() completes."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig()
        cache = PayloadCache(db_path=temp_db_path, config=config)

        # Event should exist but not be set yet
        assert hasattr(cache, '_initialized')
        assert not cache._initialized.is_set()

        # Initialize
        cache.initialize()

        # Event should now be set
        assert cache._initialized.is_set()

        cache.close()
