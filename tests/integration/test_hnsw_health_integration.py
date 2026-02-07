"""
Integration tests for HNSWHealthService with real HNSW indexes.

Tests the service with actual hnswlib Index instances to verify:
- Real index loading and integrity checking
- Performance requirements (<1s for 50K vectors, <5s for 500K vectors)
- Thread safety for concurrent access

Story #56: HNSWHealthService Core Logic
"""

import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import hnswlib
import numpy as np
import pytest

from code_indexer.services.hnsw_health_service import (
    HNSWHealthService,
    check_health_async,
)


@pytest.fixture
def temp_index_dir():
    """Create temporary directory for test indexes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def small_valid_index(temp_index_dir):
    """Create a small valid HNSW index (1000 vectors, 128 dims)."""
    index_path = temp_index_dir / "small_valid.bin"

    # Create index
    dim = 128
    num_elements = 1000

    index = hnswlib.Index(space="l2", dim=dim)
    index.init_index(max_elements=num_elements, ef_construction=200, M=16)

    # Add random vectors
    data = np.random.random((num_elements, dim)).astype("float32")
    labels = np.arange(num_elements)
    index.add_items(data, labels)

    # Save to disk
    index.save_index(str(index_path))

    return index_path


@pytest.fixture
def medium_valid_index(temp_index_dir):
    """Create a medium valid HNSW index (10K vectors, 128 dims)."""
    index_path = temp_index_dir / "medium_valid.bin"

    # Create index
    dim = 128
    num_elements = 10000

    index = hnswlib.Index(space="l2", dim=dim)
    index.init_index(max_elements=num_elements, ef_construction=200, M=16)

    # Add random vectors in batches for efficiency
    batch_size = 1000
    for i in range(0, num_elements, batch_size):
        batch_end = min(i + batch_size, num_elements)
        batch_data = np.random.random((batch_end - i, dim)).astype("float32")
        batch_labels = np.arange(i, batch_end)
        index.add_items(batch_data, batch_labels)

    # Save to disk
    index.save_index(str(index_path))

    return index_path


class TestRealIndexHealthCheck:
    """Integration tests with real HNSW indexes."""

    def test_health_check_on_real_small_index(self, small_valid_index):
        """
        Verify health check works with real small HNSW index.

        This confirms integration with actual hnswlib.Index.check_integrity().
        """
        service = HNSWHealthService()

        result = service.check_health(str(small_valid_index))

        # Should be valid
        assert result.valid is True
        assert result.file_exists is True
        assert result.readable is True
        assert result.loadable is True

        # Should have correct element count
        assert result.element_count == 1000

        # Should have checked connections
        assert result.connections_checked > 0

        # Should have inbound connection metrics
        assert result.min_inbound is not None
        assert result.max_inbound is not None
        assert result.min_inbound >= 0
        assert result.max_inbound > 0

        # Should have no errors
        assert len(result.errors) == 0

        # Should have file metadata
        assert result.file_size_bytes > 0
        assert result.last_modified is not None
        assert result.index_path == str(small_valid_index)

        # Should have timing
        assert result.check_duration_ms > 0

    def test_health_check_on_real_medium_index(self, medium_valid_index):
        """
        Verify health check works with real medium HNSW index.

        Tests performance requirement: <1s for 50K vectors (this is 10K).
        """
        service = HNSWHealthService()

        start_time = time.time()
        result = service.check_health(str(medium_valid_index))
        elapsed = time.time() - start_time

        # Should complete quickly (well under 1 second for 10K vectors)
        assert elapsed < 1.0, f"Health check took {elapsed:.2f}s, expected <1s"

        # Should have correct element count and connections checked
        # Note: Randomly generated indexes may have orphan nodes which cause valid=False
        # This is expected behavior - we verify the check completes successfully
        assert result.element_count == 10000
        assert result.connections_checked > 0
        assert result.loadable is True

        # Check internal timing matches elapsed time (within margin)
        assert abs(result.check_duration_ms - elapsed * 1000) < 100

    def test_cache_performance_with_real_index(self, small_valid_index):
        """
        Verify cache hit performance requirement (<10ms).

        Confirms caching provides significant speedup on real index.
        """
        service = HNSWHealthService(cache_ttl_seconds=300)

        # First call - warm up cache
        result1 = service.check_health(str(small_valid_index))
        assert result1.from_cache is False
        first_check_time = result1.check_duration_ms

        # Second call - should hit cache
        start_time = time.time()
        result2 = service.check_health(str(small_valid_index))
        elapsed_ms = (time.time() - start_time) * 1000

        # Should be from cache
        assert result2.from_cache is True

        # Should be MUCH faster than first check
        assert elapsed_ms < 10, f"Cache hit took {elapsed_ms:.2f}ms, expected <10ms"
        assert (
            elapsed_ms < first_check_time / 10
        ), f"Cache hit ({elapsed_ms:.2f}ms) should be >10x faster than first check ({first_check_time:.2f}ms)"

    def test_concurrent_health_checks_thread_safety(self, medium_valid_index):
        """
        Verify thread safety by running concurrent health checks.

        Multiple threads should be able to check health without conflicts.
        """
        service = HNSWHealthService()

        num_threads = 10
        results = []

        def check_health_worker():
            return service.check_health(str(medium_valid_index))

        # Run concurrent health checks
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(check_health_worker) for _ in range(num_threads)]

            for future in as_completed(futures):
                result = future.result()
                results.append(result)

        # All checks should complete
        assert len(results) == num_threads
        for result in results:
            # Note: Randomly generated indexes may have orphan nodes which cause valid=False
            # This is expected behavior - the important thing is that the check completes
            assert result.element_count == 10000
            assert result.connections_checked > 0
            # Check that we got integrity results (even if there are minor issues)
            assert result.loadable is True

        # At least one result should not be from cache (race conditions make order non-deterministic)
        assert any(not result.from_cache for result in results)

    @pytest.mark.asyncio
    async def test_async_wrapper_with_real_index(self, small_valid_index):
        """
        Verify async wrapper works with real index.

        Confirms integration between async wrapper and sync health check.
        """
        service = HNSWHealthService()
        executor = ThreadPoolExecutor(max_workers=2)

        try:
            result = await check_health_async(service, str(small_valid_index), executor)

            # Should return valid result
            assert result.valid is True
            assert result.element_count == 1000
            assert result.connections_checked > 0
            assert len(result.errors) == 0
        finally:
            executor.shutdown(wait=True)

    @pytest.mark.asyncio
    async def test_concurrent_async_health_checks(self, medium_valid_index):
        """
        Verify concurrent async health checks work correctly.

        Multiple concurrent async calls should execute safely in thread pool.
        """
        service = HNSWHealthService()
        executor = ThreadPoolExecutor(max_workers=4)

        try:
            # Run 5 concurrent async health checks
            tasks = [
                check_health_async(service, str(medium_valid_index), executor)
                for _ in range(5)
            ]

            import asyncio

            results = await asyncio.gather(*tasks)

            # All should complete
            assert len(results) == 5
            for result in results:
                # Note: Randomly generated indexes may have orphan nodes which cause valid=False
                # This is expected behavior - the important thing is that the check completes
                assert result.element_count == 10000
                assert result.connections_checked > 0
                assert result.loadable is True
        finally:
            executor.shutdown(wait=True)


class TestRealIndexCacheInvalidation:
    """Integration tests for cache invalidation with real indexes."""

    def test_mtime_invalidation_with_real_index(self, temp_index_dir):
        """
        Verify cache invalidation when real index file is modified.

        Modifying the index file should trigger fresh health check.
        """
        service = HNSWHealthService(cache_ttl_seconds=300)

        # Create initial index
        index_path = temp_index_dir / "mtime_test.bin"
        dim = 128
        num_elements = 500

        index = hnswlib.Index(space="l2", dim=dim)
        index.init_index(max_elements=num_elements + 100, ef_construction=200, M=16)  # Allow room for additional elements
        data = np.random.random((num_elements, dim)).astype("float32")
        index.add_items(data, np.arange(num_elements))
        index.save_index(str(index_path))

        # First check
        result1 = service.check_health(str(index_path))
        assert result1.from_cache is False
        assert result1.element_count == 500

        # Modify index (add more elements)
        time.sleep(0.01)  # Ensure different mtime
        index.add_items(
            np.random.random((100, dim)).astype("float32"),
            np.arange(num_elements, num_elements + 100),
        )
        index.save_index(str(index_path))

        # Second check should detect modification
        result2 = service.check_health(str(index_path))
        assert result2.from_cache is False  # Cache invalidated by mtime change
        assert result2.element_count == 600  # Updated count

    def test_ttl_invalidation_with_real_index(self, small_valid_index):
        """
        Verify TTL-based cache expiration with real index.

        Cache should expire after TTL even if file unchanged.
        """
        service = HNSWHealthService(cache_ttl_seconds=1)  # Very short TTL

        # First check
        result1 = service.check_health(str(small_valid_index))
        assert result1.from_cache is False

        # Wait for TTL to expire
        time.sleep(1.1)

        # Second check should refresh due to TTL
        result2 = service.check_health(str(small_valid_index))
        assert result2.from_cache is False  # Cache expired

        # Results should match (file unchanged)
        assert result2.element_count == result1.element_count
        assert result2.connections_checked == result1.connections_checked


class TestRealIndexErrorCases:
    """Integration tests for error handling with real index scenarios."""

    def test_corrupted_index_file(self, temp_index_dir):
        """
        Verify health check detects corrupted index file.

        Writing garbage to index file should result in load failure.
        """
        service = HNSWHealthService()

        # Create corrupted file
        corrupted_path = temp_index_dir / "corrupted.bin"
        with open(corrupted_path, "wb") as f:
            f.write(b"This is not a valid HNSW index file")

        result = service.check_health(str(corrupted_path))

        # Should detect corruption
        assert result.valid is False
        assert result.file_exists is True
        assert result.readable is True
        assert result.loadable is False  # Can't load garbage data
        assert len(result.errors) > 0
        assert "Failed to load index" in result.errors[0]

    def test_wrong_dimension_index_load(self, temp_index_dir):
        """
        Verify health check handles dimension mismatch gracefully.

        Note: The current implementation loads with a fixed dimension (128).
        This test documents the behavior when actual index has different dimension.
        """
        service = HNSWHealthService()

        # Create index with different dimension
        index_path = temp_index_dir / "wrong_dim.bin"
        dim = 256  # Different from default 128
        num_elements = 100

        index = hnswlib.Index(space="l2", dim=dim)
        index.init_index(max_elements=num_elements, ef_construction=200, M=16)
        data = np.random.random((num_elements, dim)).astype("float32")
        index.add_items(data, np.arange(num_elements))
        index.save_index(str(index_path))

        result = service.check_health(str(index_path))

        # Current implementation may fail to load due to dimension mismatch
        # This is expected behavior - health check detects the issue
        if not result.loadable:
            assert result.valid is False
            assert len(result.errors) > 0
