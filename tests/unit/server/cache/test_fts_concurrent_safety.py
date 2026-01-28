"""
Concurrent stress tests for FTS cache thread safety verification.

Story #49: Thread-Safe Cache Infrastructure Verification (AC2)

Validates that FTS cache is thread-safe:
- RLock protection prevents data corruption
- Concurrent get_or_load operations are safe
- Tantivy index reload() is thread-safe under concurrent access
- Mixed operations don't cause race conditions
"""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import MagicMock

from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
)


class TestFTSCacheThreadSafety:
    """
    Test FTS cache thread safety under concurrent access (AC2).

    Thread Safety Guarantees (verified via code analysis):
    - All cache access protected by RLock (self._cache_lock)
    - get_or_load(): Protected by 'with self._cache_lock:'
    - invalidate(): Protected by 'with self._cache_lock:'
    - clear(): Protected by 'with self._cache_lock:'
    - get_stats(): Protected by 'with self._cache_lock:'

    Tantivy-Specific Considerations:
    - reload() is called while holding the lock (atomic access)
    - Tantivy readers are thread-safe for concurrent search operations
    """

    def _create_fts_loader(self, index_name: str) -> Callable[[], Tuple[Any, Any]]:
        """Create a mock FTS loader function."""

        def loader() -> Tuple[Any, Any]:
            time.sleep(0.001)  # Simulate I/O
            mock_index = MagicMock()
            mock_index.index_name = index_name
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()
            return mock_index, mock_schema

        return loader

    def test_concurrent_get_operations_same_key(self, tmp_path: Path) -> None:
        """
        Test concurrent FTS get_or_load on same index returns consistent results.

        10 simultaneous queries should all get the same cached object.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=True)
        cache = FTSIndexCache(config=config)

        index_dir = str(tmp_path / "fts_index")
        results: List[Tuple[Any, Any]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def query_cache() -> None:
            try:
                result = cache.get_or_load(index_dir, self._create_fts_loader("test"))
                with lock:
                    results.append(result)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=query_cache) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(exceptions) == 0, f"Concurrent queries raised exceptions: {exceptions}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

        first_index, first_schema = results[0]
        for index, schema in results[1:]:
            assert index is first_index, "All queries should return same cached index"
            assert schema is first_schema, "All queries should return same cached schema"

        stats = cache.get_stats()
        assert stats.reload_count >= 9, "Should have at least 9 reload calls"

    def test_concurrent_different_indexes(self, tmp_path: Path) -> None:
        """
        Test concurrent queries on different FTS indexes don't cause corruption.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        num_indexes = 5
        queries_per_index = 10
        results: Dict[str, List[Tuple[Any, Any]]] = {
            f"idx_{i}": [] for i in range(num_indexes)
        }
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def query_index(idx: int) -> None:
            index_dir = str(tmp_path / f"fts_{idx}")
            index_name = f"idx_{idx}"
            try:
                result = cache.get_or_load(
                    index_dir, self._create_fts_loader(index_name)
                )
                with lock:
                    results[index_name].append(result)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = []
        for idx in range(num_indexes):
            for _ in range(queries_per_index):
                threads.append(threading.Thread(target=query_index, args=(idx,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Concurrent queries raised exceptions: {exceptions}"

        for index_name, index_results in results.items():
            assert len(index_results) == queries_per_index, (
                f"Index {index_name}: expected {queries_per_index}, got {len(index_results)}"
            )

    def test_tantivy_reload_thread_safety(self, tmp_path: Path) -> None:
        """
        Test that Tantivy reload() calls are thread-safe under concurrent access.

        With reload_on_access=True, concurrent cache hits trigger reload() which
        must be thread-safe and not corrupt internal state.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=True)
        cache = FTSIndexCache(config=config)

        index_dir = str(tmp_path / "fts_index")
        cache.get_or_load(index_dir, self._create_fts_loader("test"))

        reload_counts: List[int] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def concurrent_query() -> None:
            try:
                cache.get_or_load(index_dir, self._create_fts_loader("test"))
                with lock:
                    reload_counts.append(1)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = [threading.Thread(target=concurrent_query) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Reload operations raised exceptions: {exceptions}"
        assert len(reload_counts) == 50, "All queries should complete"

        stats = cache.get_stats()
        assert stats.reload_count >= 50, "All cache hits should trigger reload"

    def test_concurrent_invalidation(self, tmp_path: Path) -> None:
        """
        Test concurrent invalidation and get_or_load operations are safe.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        index_dir = str(tmp_path / "fts_index")
        cache.get_or_load(index_dir, self._create_fts_loader("test"))

        operations_completed: List[Tuple[str, int]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def read_operation(op_id: int) -> None:
            try:
                cache.get_or_load(index_dir, self._create_fts_loader("test"))
                with lock:
                    operations_completed.append(("read", op_id))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        def invalidate_operation(op_id: int) -> None:
            try:
                cache.invalidate(index_dir)
                with lock:
                    operations_completed.append(("invalidate", op_id))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = []
        for i in range(20):
            if i % 5 == 0:
                threads.append(threading.Thread(target=invalidate_operation, args=(i,)))
            else:
                threads.append(threading.Thread(target=read_operation, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Operations raised exceptions: {exceptions}"
        assert len(operations_completed) == 20, "All operations should complete"
