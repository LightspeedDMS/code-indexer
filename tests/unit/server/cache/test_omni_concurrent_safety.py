"""
Concurrent stress tests for OmniCache thread safety verification.

Story #49: Thread-Safe Cache Infrastructure Verification (AC3)

Validates that OmniCache is thread-safe:
- RLock protection prevents data corruption
- Concurrent store_results operations are safe
- Concurrent get_results operations are safe
- LRU eviction is atomic under concurrent additions
- Cache coherency is maintained
"""

import threading
from typing import Any, Dict, List, Tuple

from code_indexer.server.omni.omni_cache import OmniCache


class TestOmniCacheThreadSafety:
    """
    Test OmniCache thread safety under concurrent access (AC3).

    Thread Safety Guarantees (verified via code analysis):
    - All cache access protected by RLock (self.lock)
    - store_results(): Protected by 'with self.lock:'
    - get_results(): Protected by 'with self.lock:'
    - get_metadata(): Protected by 'with self.lock:'
    - get_stats(): Protected by 'with self.lock:'

    Cache Coherency Guarantees:
    - LRU eviction handled by cachetools.TTLCache (thread-safe when protected)
    - All operations are atomic within lock scope
    """

    def test_concurrent_store_operations(self) -> None:
        """
        Test concurrent store_results operations don't cause data corruption.
        """
        cache = OmniCache(ttl_seconds=60, max_entries=100)

        cursors: List[str] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def store_results(idx: int) -> None:
            try:
                results = [{"id": i, "data": f"result_{idx}_{i}"} for i in range(10)]
                cursor = cache.store_results(results, {"query_idx": idx})
                with lock:
                    cursors.append(cursor)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=store_results, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(exceptions) == 0, f"Store operations raised exceptions: {exceptions}"
        assert len(cursors) == 20, f"Expected 20 cursors, got {len(cursors)}"
        assert len(set(cursors)) == 20, "All cursors should be unique"

        stats = cache.get_stats()
        assert stats["total_entries"] == 20, "Cache should have 20 entries"

    def test_concurrent_get_operations_same_cursor(self) -> None:
        """
        Test concurrent get_results on same cursor returns consistent results.
        """
        cache = OmniCache(ttl_seconds=60, max_entries=100)

        test_results = [{"id": i, "data": f"value_{i}"} for i in range(100)]
        cursor = cache.store_results(test_results, {"test": True})

        retrieved: List[List[Dict]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def get_results() -> None:
            try:
                results = cache.get_results(cursor, offset=0, limit=10)
                with lock:
                    if results is not None:
                        retrieved.append(results)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=get_results) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(exceptions) == 0, f"Get operations raised exceptions: {exceptions}"
        assert len(retrieved) == 20, f"Expected 20 results, got {len(retrieved)}"

        first_result = retrieved[0]
        for result in retrieved[1:]:
            assert result == first_result, "All gets should return same results"

    def test_concurrent_mixed_operations(self) -> None:
        """
        Test concurrent store and get operations don't cause corruption.
        """
        cache = OmniCache(ttl_seconds=60, max_entries=100)

        initial_cursors = []
        for i in range(5):
            cursor = cache.store_results(
                [{"id": j, "pre_data": f"pre_{i}_{j}"} for j in range(10)],
                {"pre_idx": i},
            )
            initial_cursors.append(cursor)

        operations: List[Tuple[str, Any]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def store_operation(idx: int) -> None:
            try:
                results = [{"id": j, "data": f"new_{idx}_{j}"} for j in range(10)]
                cursor = cache.store_results(results, {"idx": idx})
                with lock:
                    operations.append(("store", cursor))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        def get_operation(cursor: str) -> None:
            try:
                results = cache.get_results(cursor, offset=0, limit=5)
                with lock:
                    operations.append(("get", results is not None))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = []
        for i in range(30):
            if i % 3 == 0:
                threads.append(threading.Thread(target=store_operation, args=(i,)))
            else:
                cursor = initial_cursors[i % len(initial_cursors)]
                threads.append(threading.Thread(target=get_operation, args=(cursor,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Mixed operations raised exceptions: {exceptions}"
        assert len(operations) == 30, "All operations should complete"

    def test_lru_eviction_under_concurrent_load(self) -> None:
        """
        Test that LRU eviction is atomic under concurrent additions.

        Cache size limit should be maintained during concurrent stores.
        """
        max_entries = 10
        cache = OmniCache(ttl_seconds=60, max_entries=max_entries)

        cursors: List[str] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def store_results(idx: int) -> None:
            try:
                results = [{"id": j, "data": f"result_{idx}_{j}"} for j in range(5)]
                cursor = cache.store_results(results, {"idx": idx})
                with lock:
                    cursors.append(cursor)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=store_results, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Store operations raised exceptions: {exceptions}"

        stats = cache.get_stats()
        assert (
            stats["total_entries"] <= max_entries
        ), f"Cache size {stats['total_entries']} exceeds max {max_entries}"

    def test_concurrent_get_metadata(self) -> None:
        """
        Test concurrent get_metadata operations are thread-safe.
        """
        cache = OmniCache(ttl_seconds=60, max_entries=100)

        test_results = [{"id": i} for i in range(50)]
        cursor = cache.store_results(test_results, {"query": "test"})

        metadata_results: List[Dict] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def get_metadata() -> None:
            try:
                metadata = cache.get_metadata(cursor)
                with lock:
                    if metadata:
                        metadata_results.append(metadata)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=get_metadata) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(exceptions) == 0, f"Get metadata raised exceptions: {exceptions}"
        assert len(metadata_results) == 20, "All metadata requests should succeed"

        first_metadata = metadata_results[0]
        for metadata in metadata_results[1:]:
            assert metadata == first_metadata, "All metadata should be identical"
