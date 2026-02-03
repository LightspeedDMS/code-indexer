"""
Concurrent stress tests for HNSW cache thread safety verification.

Story #49: Thread-Safe Cache Infrastructure Verification (AC1)

Validates that HNSW cache is thread-safe:
- RLock protection prevents data corruption
- Concurrent get_or_load operations are safe
- Concurrent invalidate operations are safe
- Mixed read/write operations don't cause race conditions
"""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)


class TestHNSWCacheThreadSafety:
    """
    Test HNSW cache thread safety under concurrent access (AC1).

    Thread Safety Guarantees (verified via code analysis):
    - All cache access protected by RLock (self._cache_lock)
    - get_or_load(): Protected by 'with self._cache_lock:'
    - invalidate(): Protected by 'with self._cache_lock:'
    - clear(): Protected by 'with self._cache_lock:'
    - get_stats(): Protected by 'with self._cache_lock:'
    """

    def _create_hnsw_loader(self, repo_name: str) -> Callable[[], Tuple[Any, Dict]]:
        """Create a mock HNSW loader function."""

        def loader() -> Tuple[Any, Dict[int, str]]:
            time.sleep(0.001)  # Simulate I/O
            mock_index = MagicMock()
            mock_index.repo_name = repo_name
            id_mapping = {i: f"{repo_name}_vec_{i}" for i in range(100)}
            return mock_index, id_mapping

        return loader

    def test_concurrent_get_operations_same_key(self, tmp_path: Path) -> None:
        """
        Test concurrent get_or_load on same cache key returns consistent results.

        10 simultaneous queries should all get the same cached object.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        repo_path = str(tmp_path / "test_repo")
        results: List[Tuple[Any, Dict]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def query_cache() -> None:
            try:
                result = cache.get_or_load(repo_path, self._create_hnsw_loader("test"))
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

        assert (
            len(exceptions) == 0
        ), f"Concurrent queries raised exceptions: {exceptions}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"

        # All results should be identical cached objects
        first_index, first_mapping = results[0]
        for index, mapping in results[1:]:
            assert index is first_index, "All queries should return same cached index"
            assert mapping is first_mapping, "All queries should return same mapping"

        stats = cache.get_stats()
        assert stats.miss_count == 1, "Should have exactly 1 miss (first query)"
        assert stats.hit_count == 9, "Should have 9 hits (subsequent queries)"

    def test_concurrent_different_repos(self, tmp_path: Path) -> None:
        """
        Test concurrent queries on different repositories don't cause corruption.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        num_repos = 5
        queries_per_repo = 10
        results: Dict[str, List[Tuple[Any, Dict]]] = {
            f"repo_{i}": [] for i in range(num_repos)
        }
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def query_repo(repo_idx: int) -> None:
            repo_path = str(tmp_path / f"repo_{repo_idx}")
            repo_name = f"repo_{repo_idx}"
            try:
                result = cache.get_or_load(
                    repo_path, self._create_hnsw_loader(repo_name)
                )
                with lock:
                    results[repo_name].append(result)
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = []
        for repo_idx in range(num_repos):
            for _ in range(queries_per_repo):
                threads.append(threading.Thread(target=query_repo, args=(repo_idx,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert (
            len(exceptions) == 0
        ), f"Concurrent queries raised exceptions: {exceptions}"

        for repo_name, repo_results in results.items():
            assert (
                len(repo_results) == queries_per_repo
            ), f"Repo {repo_name}: expected {queries_per_repo}, got {len(repo_results)}"
            first_index = repo_results[0][0]
            for idx, (index, _) in enumerate(repo_results[1:]):
                assert (
                    index is first_index
                ), f"Repo {repo_name}: result {idx + 1} has different index object"

        assert cache.get_stats().cached_repositories == num_repos

    def test_concurrent_invalidation(self, tmp_path: Path) -> None:
        """
        Test concurrent invalidation and get_or_load operations are safe.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        repo_path = str(tmp_path / "test_repo")
        cache.get_or_load(repo_path, self._create_hnsw_loader("test"))

        operations_completed: List[Tuple[str, int]] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def read_operation(op_id: int) -> None:
            try:
                cache.get_or_load(repo_path, self._create_hnsw_loader("test"))
                with lock:
                    operations_completed.append(("read", op_id))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        def invalidate_operation(op_id: int) -> None:
            try:
                cache.invalidate(repo_path)
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

    def test_concurrent_clear_operations(self, tmp_path: Path) -> None:
        """
        Test concurrent clear and get_or_load operations are safe.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        exceptions: List[Exception] = []
        lock = threading.Lock()

        def add_and_query(idx: int) -> None:
            try:
                repo_path = str(tmp_path / f"repo_{idx}")
                cache.get_or_load(repo_path, self._create_hnsw_loader(f"repo_{idx}"))
            except Exception as e:
                with lock:
                    exceptions.append(e)

        def clear_cache() -> None:
            try:
                cache.clear()
            except Exception as e:
                with lock:
                    exceptions.append(e)

        threads = []
        for i in range(30):
            if i % 10 == 5:
                threads.append(threading.Thread(target=clear_cache))
            else:
                threads.append(threading.Thread(target=add_and_query, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Operations raised exceptions: {exceptions}"
