"""
Parameterized stress tests for cache thread safety verification.

Story #49: Thread-Safe Cache Infrastructure Verification (AC4)

Validates thread safety with varying thread counts and operation patterns:
- Parameterized tests for 5, 10, 20 concurrent threads
- Mixed operation patterns (read-heavy, write-heavy)
- Thread contention metrics for performance analysis
- All tests complete within 30 seconds total
"""

import statistics
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
)
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.server.omni.omni_cache import OmniCache


class TestParameterizedStressTests:
    """Parameterized stress tests for varying thread counts (5, 10, 20)."""

    @pytest.mark.parametrize("num_threads", [5, 10, 20])
    def test_hnsw_cache_concurrent_stress(
        self, tmp_path: Path, num_threads: int
    ) -> None:
        """Stress test HNSW cache with parameterized thread counts."""
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        operations_per_thread = 20
        results: List[bool] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def stress_worker(worker_id: int) -> None:
            for op_id in range(operations_per_thread):
                try:
                    repo_idx = op_id % 5
                    repo_path = str(tmp_path / f"repo_{repo_idx}")

                    def loader() -> Tuple[Any, Dict]:
                        time.sleep(0.001)
                        return MagicMock(), {i: f"v{i}" for i in range(10)}

                    cache.get_or_load(repo_path, loader)
                    with lock:
                        results.append(True)
                except Exception as e:
                    with lock:
                        exceptions.append(e)
                        results.append(False)

        start_time = time.time()
        threads = [
            threading.Thread(target=stress_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        duration = time.time() - start_time
        total_ops = num_threads * operations_per_thread
        success_count = sum(results)

        assert len(exceptions) == 0, f"Raised {len(exceptions)} exceptions"
        assert success_count == total_ops, f"Expected {total_ops}, got {success_count}"
        assert duration < 30, f"Took {duration:.2f}s, expected <30s"

    @pytest.mark.parametrize("num_threads", [5, 10, 20])
    def test_fts_cache_concurrent_stress(
        self, tmp_path: Path, num_threads: int
    ) -> None:
        """Stress test FTS cache with parameterized thread counts."""
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        operations_per_thread = 20
        results: List[bool] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def stress_worker(worker_id: int) -> None:
            for op_id in range(operations_per_thread):
                try:
                    idx = op_id % 5
                    index_dir = str(tmp_path / f"fts_{idx}")

                    def loader() -> Tuple[Any, Any]:
                        time.sleep(0.001)
                        mock_idx = MagicMock()
                        mock_idx.reload = MagicMock()
                        return mock_idx, MagicMock()

                    cache.get_or_load(index_dir, loader)
                    with lock:
                        results.append(True)
                except Exception as e:
                    with lock:
                        exceptions.append(e)
                        results.append(False)

        start_time = time.time()
        threads = [
            threading.Thread(target=stress_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        duration = time.time() - start_time
        total_ops = num_threads * operations_per_thread
        success_count = sum(results)

        assert len(exceptions) == 0, f"Raised {len(exceptions)} exceptions"
        assert success_count == total_ops, f"Expected {total_ops}, got {success_count}"
        assert duration < 30, f"Took {duration:.2f}s, expected <30s"

    @pytest.mark.parametrize("num_threads", [5, 10, 20])
    def test_omni_cache_concurrent_stress(self, num_threads: int) -> None:
        """Stress test OmniCache with parameterized thread counts."""
        cache = OmniCache(ttl_seconds=60, max_entries=50)

        operations_per_thread = 20
        results: List[bool] = []
        exceptions: List[Exception] = []
        lock = threading.Lock()
        stored_cursors: List[str] = []

        def stress_worker(worker_id: int) -> None:
            for op_id in range(operations_per_thread):
                try:
                    if op_id % 3 == 0:
                        data = [{"id": j, "w": worker_id, "op": op_id} for j in range(5)]
                        cursor = cache.store_results(data, {"worker": worker_id})
                        with lock:
                            stored_cursors.append(cursor)
                            results.append(True)
                    else:
                        with lock:
                            cursor = stored_cursors[
                                (worker_id + op_id) % len(stored_cursors)
                            ] if stored_cursors else None
                        if cursor:
                            cache.get_results(cursor, offset=0, limit=5)
                        with lock:
                            results.append(True)
                except Exception as e:
                    with lock:
                        exceptions.append(e)
                        results.append(False)

        start_time = time.time()
        threads = [
            threading.Thread(target=stress_worker, args=(i,))
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        duration = time.time() - start_time
        total_ops = num_threads * operations_per_thread
        success_count = sum(results)

        assert len(exceptions) == 0, f"Raised {len(exceptions)} exceptions"
        assert success_count == total_ops, f"Expected {total_ops}, got {success_count}"
        assert duration < 30, f"Took {duration:.2f}s, expected <30s"


class TestMixedOperationPatterns:
    """Test mixed operation patterns (read-heavy, write-heavy) per AC4."""

    def test_read_heavy_pattern_hnsw(self, tmp_path: Path) -> None:
        """Test HNSW cache with 90% reads, 10% writes."""
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        repo_paths = [str(tmp_path / f"repo_{i}") for i in range(5)]
        for path in repo_paths:
            cache.get_or_load(
                path, lambda p=path: (MagicMock(), {i: f"{p}_v{i}" for i in range(10)})
            )

        total_ops = 100
        read_ops = 0
        write_ops = 0
        exceptions: List[Exception] = []
        lock = threading.Lock()

        def read_heavy_worker(worker_id: int) -> None:
            nonlocal read_ops, write_ops
            for op_id in range(20):
                try:
                    if op_id % 10 == 0:
                        cache.invalidate(repo_paths[op_id % len(repo_paths)])
                        with lock:
                            write_ops += 1
                    else:
                        path = repo_paths[op_id % len(repo_paths)]
                        cache.get_or_load(
                            path,
                            lambda p=path: (MagicMock(), {i: f"{p}_v{i}" for i in range(10)}),
                        )
                        with lock:
                            read_ops += 1
                except Exception as e:
                    with lock:
                        exceptions.append(e)

        threads = [
            threading.Thread(target=read_heavy_worker, args=(i,)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Read-heavy raised exceptions: {exceptions}"
        total = read_ops + write_ops
        assert total == total_ops, f"Expected {total_ops} ops, got {total}"
        read_ratio = read_ops / total
        assert read_ratio >= 0.85, f"Read ratio {read_ratio:.2%} should be ~90%"

    def test_write_heavy_pattern_omni(self) -> None:
        """Test OmniCache with 70% writes, 30% reads."""
        cache = OmniCache(ttl_seconds=60, max_entries=100)

        total_ops = 100
        write_ops = 0
        read_ops = 0
        exceptions: List[Exception] = []
        lock = threading.Lock()
        cursors: List[str] = []

        def write_heavy_worker(worker_id: int) -> None:
            nonlocal write_ops, read_ops
            for op_id in range(20):
                try:
                    if op_id % 10 < 7:
                        data = [{"id": i, "w": worker_id} for i in range(5)]
                        cursor = cache.store_results(data, {"w": worker_id})
                        with lock:
                            cursors.append(cursor)
                            write_ops += 1
                    else:
                        with lock:
                            cursor = cursors[-1] if cursors else None
                        if cursor:
                            cache.get_results(cursor, offset=0, limit=5)
                        with lock:
                            read_ops += 1
                except Exception as e:
                    with lock:
                        exceptions.append(e)

        threads = [
            threading.Thread(target=write_heavy_worker, args=(i,)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(exceptions) == 0, f"Write-heavy raised exceptions: {exceptions}"
        total = read_ops + write_ops
        assert total == total_ops, f"Expected {total_ops} ops, got {total}"
        write_ratio = write_ops / total
        assert write_ratio >= 0.65, f"Write ratio {write_ratio:.2%} should be ~70%"


class TestThreadContentionMetrics:
    """Tests measuring thread contention under load for performance analysis."""

    def test_measure_hnsw_contention(self, tmp_path: Path) -> None:
        """Measure thread contention in HNSW cache under concurrent load."""
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        repo_path = str(tmp_path / "test_repo")
        operation_times: List[float] = []
        lock = threading.Lock()

        def timed_operation() -> None:
            start = time.perf_counter()
            cache.get_or_load(
                repo_path, lambda: (MagicMock(), {i: f"v{i}" for i in range(10)})
            )
            elapsed = time.perf_counter() - start
            with lock:
                operation_times.append(elapsed)

        threads = [threading.Thread(target=timed_operation) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(operation_times) == 20, "All operations should complete"
        avg_time = statistics.mean(operation_times)
        max_time = max(operation_times)

        assert avg_time < 0.1, f"Average time {avg_time:.3f}s too high"
        assert max_time < 1.0, f"Max time {max_time:.3f}s indicates contention"

    def test_measure_omni_contention(self) -> None:
        """Measure thread contention in OmniCache under concurrent load."""
        cache = OmniCache(ttl_seconds=60, max_entries=100)
        cursor = cache.store_results([{"id": i} for i in range(100)], {"test": True})

        operation_times: List[float] = []
        lock = threading.Lock()

        def timed_operation() -> None:
            start = time.perf_counter()
            cache.get_results(cursor, offset=0, limit=10)
            elapsed = time.perf_counter() - start
            with lock:
                operation_times.append(elapsed)

        threads = [threading.Thread(target=timed_operation) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(operation_times) == 50, "All operations should complete"
        avg_time = statistics.mean(operation_times)
        max_time = max(operation_times)

        assert avg_time < 0.01, f"Average time {avg_time:.6f}s too high"
        assert max_time < 0.1, f"Max time {max_time:.6f}s indicates contention"
