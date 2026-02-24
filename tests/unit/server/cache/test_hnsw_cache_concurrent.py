"""
Concurrent tests for HNSWIndexCache per-key Event sentinel pattern.

Story #277: Non-Blocking HNSW/FTS Cache Population for Concurrent Multi-Repo Search

Verifies that get_or_load() releases the global lock during disk I/O, enabling:
- Parallel cache population for different keys (not serialized)
- Load deduplication for same key (only one thread calls loader)
- Proper failure propagation and sentinel cleanup
- No deadlocks under concurrent load/invalidate/cleanup

These tests are written FIRST (TDD RED phase). They will FAIL against the
current implementation that holds the global lock during the loader() call,
because that serializes all concurrent loads regardless of key.
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)


class TestHNSWCacheParallelLoads:
    """Tests verifying that loads for different keys run in parallel (not serialized)."""

    def _make_slow_loader(
        self,
        delay: float,
        index_name: str = "mock_index",
    ):
        """Create a slow loader that simulates disk I/O."""

        def loader() -> Tuple[Any, Dict[int, str]]:
            time.sleep(delay)
            mock_index = MagicMock()
            mock_index.name = index_name
            return mock_index, {0: f"{index_name}_vec_0"}

        return loader

    def test_parallel_loads_different_keys(self, tmp_path: Path) -> None:
        """
        Two threads load different keys simultaneously. Both complete in parallel,
        not in serial. Wall time must be < 2x single load time.

        With the old locking pattern (hold lock during load), both loads are
        serialized: total time >= 2x single load time.
        With the sentinel pattern (lock released during I/O), both loads run
        in parallel: total time ~= single load time.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        key_a = str(tmp_path / "repo_a")
        key_b = str(tmp_path / "repo_b")
        load_delay = 0.3  # 300ms per load - clear signal for serialization vs parallel

        results: List[Optional[Tuple[Any, Dict]]] = [None, None]
        errors: List[Optional[Exception]] = [None, None]

        # Barrier ensures both threads start loading at exactly the same time
        barrier = threading.Barrier(2)

        def load_a():
            barrier.wait()
            try:
                results[0] = cache.get_or_load(key_a, self._make_slow_loader(load_delay, "index_a"))
            except Exception as e:
                errors[0] = e

        def load_b():
            barrier.wait()
            try:
                results[1] = cache.get_or_load(key_b, self._make_slow_loader(load_delay, "index_b"))
            except Exception as e:
                errors[1] = e

        thread_a = threading.Thread(target=load_a)
        thread_b = threading.Thread(target=load_b)

        start_time = time.monotonic()
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5.0)
        thread_b.join(timeout=5.0)
        wall_time = time.monotonic() - start_time

        assert errors[0] is None, f"Thread A raised: {errors[0]}"
        assert errors[1] is None, f"Thread B raised: {errors[1]}"
        assert results[0] is not None, "Thread A got no result"
        assert results[1] is not None, "Thread B got no result"

        # Parallel: wall time should be < 1.8x single load time
        # Serial: wall time would be >= 2x single load time (~0.6s+)
        max_allowed_time = load_delay * 1.8
        assert wall_time < max_allowed_time, (
            f"Loads were serialized: wall_time={wall_time:.3f}s, "
            f"expected < {max_allowed_time:.3f}s (2 parallel loads of {load_delay}s each)"
        )

    def test_deduplicated_load_same_key(self, tmp_path: Path) -> None:
        """
        Two threads request the same key. Only one thread calls loader().
        The second thread waits and receives the result from the first thread's load.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        key = str(tmp_path / "repo_shared")
        load_count = 0
        load_lock = threading.Lock()

        def counting_loader() -> Tuple[Any, Dict[int, str]]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.2)  # Slow enough that both threads start before one finishes
            mock_index = MagicMock()
            return mock_index, {0: "shared_vec"}

        results: List[Optional[Tuple[Any, Dict]]] = [None, None]
        barrier = threading.Barrier(2)

        def load_thread(idx: int):
            barrier.wait()
            results[idx] = cache.get_or_load(key, counting_loader)

        threads = [threading.Thread(target=load_thread, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Loader must be called exactly once (deduplication)
        assert load_count == 1, f"Expected loader called once, got {load_count}"

        # Both threads must get the same result
        assert results[0] is not None
        assert results[1] is not None
        index_0, mapping_0 = results[0]
        index_1, mapping_1 = results[1]
        assert index_0 is index_1, "Both threads must receive the same index object"
        assert mapping_0 is mapping_1, "Both threads must receive the same id_mapping"

    def test_waiter_receives_loaded_result(self, tmp_path: Path) -> None:
        """
        First thread loads, second thread waits on the Event.
        Both threads receive identical results (same objects, not copies).
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        key = str(tmp_path / "repo_waited")
        mock_index = MagicMock()
        mock_index.name = "waited_index"

        # Track when loader starts to ensure thread B starts while A is still loading
        loader_started = threading.Event()

        def slow_loader() -> Tuple[Any, Dict[int, str]]:
            loader_started.set()
            time.sleep(0.2)
            return mock_index, {0: "waited_vec", 1: "waited_vec2"}

        results: List[Optional[Tuple[Any, Dict]]] = [None, None]

        def thread_a():
            results[0] = cache.get_or_load(key, slow_loader)

        def thread_b():
            # Wait until thread A has started loading, then race to get same key
            loader_started.wait(timeout=2.0)
            results[1] = cache.get_or_load(key, slow_loader)

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)

        ta.start()
        tb.start()
        ta.join(timeout=5.0)
        tb.join(timeout=5.0)

        assert results[0] is not None, "Thread A got no result"
        assert results[1] is not None, "Thread B got no result"

        index_a, mapping_a = results[0]
        index_b, mapping_b = results[1]

        # Both must be the exact same objects
        assert index_a is mock_index, "Thread A must get the mock index"
        assert index_b is mock_index, "Thread B must get the same mock index"
        assert index_a is index_b, "Both threads must see the same index object"
        assert mapping_a is mapping_b, "Both threads must see the same id_mapping"

    def test_cache_hit_not_blocked_by_loading(self, tmp_path: Path) -> None:
        """
        Thread A is loading key-B (slow). Thread C requests already-cached key-A.
        Thread C must return from cache immediately without waiting for thread A.

        With the old global lock pattern, thread C would block while thread A
        holds the lock during I/O. With the sentinel pattern, thread C only needs
        the lock for the fast dict lookup.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        key_cached = str(tmp_path / "repo_cached")
        key_loading = str(tmp_path / "repo_loading")

        # Pre-populate key_cached
        mock_cached_index = MagicMock()
        mock_cached_index.name = "cached"
        cache.get_or_load(key_cached, lambda: (mock_cached_index, {0: "cached_vec"}))

        load_delay = 0.4  # 400ms - clearly detectable if thread C has to wait
        loader_started = threading.Event()

        def slow_loader_for_b() -> Tuple[Any, Dict[int, str]]:
            loader_started.set()
            time.sleep(load_delay)
            return MagicMock(), {0: "new_vec"}

        hit_duration: List[float] = []

        def thread_loading_b():
            cache.get_or_load(key_loading, slow_loader_for_b)

        def thread_cache_hit_c():
            # Wait for thread_a to start loading key_b (sentinel planted)
            loader_started.wait(timeout=2.0)
            start = time.monotonic()
            result = cache.get_or_load(key_cached, lambda: (MagicMock(), {}))
            duration = time.monotonic() - start
            hit_duration.append(duration)
            assert result[0] is mock_cached_index

        ta = threading.Thread(target=thread_loading_b)
        tc = threading.Thread(target=thread_cache_hit_c)

        ta.start()
        tc.start()
        ta.join(timeout=5.0)
        tc.join(timeout=5.0)

        assert len(hit_duration) == 1, "Thread C did not complete"
        # Cache hit should take << load_delay (not blocked by ongoing load)
        max_hit_duration = load_delay * 0.25  # Should be much faster than the load
        assert hit_duration[0] < max_hit_duration, (
            f"Cache hit was blocked by ongoing load: "
            f"hit_duration={hit_duration[0]:.3f}s, expected < {max_hit_duration:.3f}s"
        )


class TestHNSWCacheLoaderFailure:
    """Tests verifying correct sentinel cleanup and failure propagation on loader errors."""

    def test_loader_failure_signals_event(self, tmp_path: Path) -> None:
        """
        When loader raises IOError, the Event must be signaled (so waiters wake up),
        the sentinel must be removed from _loading, and the exception must propagate.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)
        key = str(tmp_path / "repo_fail")

        def failing_loader() -> Tuple[Any, Dict]:
            time.sleep(0.05)
            raise IOError("Disk read failed")

        with pytest.raises(IOError, match="Disk read failed"):
            cache.get_or_load(key, failing_loader)

        # Sentinel must be removed after failure
        assert not hasattr(cache, "_loading") or key not in getattr(cache, "_loading", {}), (
            "Sentinel (_loading[key]) must be removed after loader failure"
        )

        # Cache must NOT contain a stale entry
        normalized_key = str(Path(key).resolve())
        assert normalized_key not in cache._cache, "Failed load must not leave a cache entry"

    def test_loader_failure_waiter_retries(self, tmp_path: Path) -> None:
        """
        Thread-1 loads and fails. Thread-2 is waiting on the Event.
        When thread-1 fails:
          - Thread-2's Event.wait() returns
          - Thread-2 finds no sentinel and no cache entry
          - Thread-2 becomes the new loader
          - If thread-2's loader succeeds, the entry is cached normally
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)
        key = str(tmp_path / "repo_retry")

        # State tracking
        call_count = 0
        call_lock = threading.Lock()
        loader_1_started = threading.Event()
        mock_index = MagicMock()
        mock_index.name = "retry_index"

        def loader_that_fails_first_time() -> Tuple[Any, Dict]:
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count

            if current_call == 1:
                # First call: signal that we started, then fail
                loader_1_started.set()
                time.sleep(0.1)  # Give thread-2 time to start waiting
                raise IOError("First load failed")
            else:
                # Second call: succeed
                time.sleep(0.05)
                return mock_index, {0: "retry_vec"}

        results_errors: List[Optional[Exception]] = [None, None]
        results_values: List[Optional[Tuple]] = [None, None]

        def thread_1():
            try:
                results_values[0] = cache.get_or_load(key, loader_that_fails_first_time)
            except Exception as e:
                results_errors[0] = e

        def thread_2():
            # Wait for thread-1 to start loading, then join as a waiter
            loader_1_started.wait(timeout=2.0)
            try:
                results_values[1] = cache.get_or_load(key, loader_that_fails_first_time)
            except Exception as e:
                results_errors[1] = e

        t1 = threading.Thread(target=thread_1)
        t2 = threading.Thread(target=thread_2)
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        # Thread-1 must have received the error
        assert results_errors[0] is not None, "Thread-1 should have received the IOError"
        assert isinstance(results_errors[0], IOError)

        # Thread-2 should have succeeded (became new loader after thread-1 failed)
        assert results_errors[1] is None, f"Thread-2 should not have raised: {results_errors[1]}"
        assert results_values[1] is not None, "Thread-2 should have gotten a result"
        result_index, result_mapping = results_values[1]
        assert result_index is mock_index, "Thread-2 must get the mock index from its own load"

    def test_loader_failure_no_stale_sentinel(self, tmp_path: Path) -> None:
        """
        After a loader failure, _loading dict must not contain the key.
        Subsequent requests must go through the normal 'first loader' path,
        not the 'waiter' path. This verifies the finally block cleans up correctly.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)
        key = str(tmp_path / "repo_no_stale")
        normalized_key = str(Path(key).resolve())

        # First attempt: fail
        def failing_loader() -> Tuple[Any, Dict]:
            raise RuntimeError("Load failed")

        with pytest.raises(RuntimeError):
            cache.get_or_load(key, failing_loader)

        # Verify no stale sentinel
        loading_dict = getattr(cache, "_loading", {})
        assert normalized_key not in loading_dict, (
            f"Stale sentinel found in _loading after failure: {loading_dict}"
        )

        # Second attempt: succeed (must work as fresh first-loader, not waiter)
        mock_index = MagicMock()
        load_count = 0

        def successful_loader() -> Tuple[Any, Dict]:
            nonlocal load_count
            load_count += 1
            return mock_index, {0: "fresh_vec"}

        result = cache.get_or_load(key, successful_loader)
        assert result[0] is mock_index, "Second attempt must succeed"
        assert load_count == 1, "Second attempt must call loader once"

        # Entry must be in cache after success
        assert normalized_key in cache._cache


class TestHNSWCacheEdgeCases:
    """Edge case tests for concurrent cache operations."""

    def test_expired_entry_with_concurrent_load(self, tmp_path: Path) -> None:
        """
        Entry exists but is expired. Two threads hit the expired entry simultaneously.
        Only one thread calls loader(); the other waits. No duplicate loads.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=0.0001)  # ~6ms TTL
        cache = HNSWIndexCache(config=config)

        key = str(tmp_path / "repo_expired")
        mock_index = MagicMock()

        # Seed the cache with an entry that will expire
        cache.get_or_load(key, lambda: (MagicMock(), {0: "old_vec"}))

        # Wait for expiry
        time.sleep(0.05)

        load_count = 0
        load_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def counting_loader() -> Tuple[Any, Dict]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.1)  # Slow enough for both threads to race
            return mock_index, {0: "fresh_vec"}

        results: List[Optional[Tuple]] = [None, None]

        def load_thread(idx: int):
            barrier.wait()
            results[idx] = cache.get_or_load(key, counting_loader)

        threads = [threading.Thread(target=load_thread, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert results[0] is not None
        assert results[1] is not None
        # Only one should call the loader (other waits as sentinel)
        assert load_count == 1, (
            f"Expected exactly 1 loader call for expired entry with 2 concurrent threads, "
            f"got {load_count}"
        )
        # Both get the same result
        assert results[0][0] is mock_index
        assert results[1][0] is mock_index

    def test_invalidate_during_load_no_deadlock(self, tmp_path: Path) -> None:
        """
        Thread A is loading key. Thread B calls invalidate(key) concurrently.
        No deadlock must occur. Thread A's result may be stored after invalidation
        (this is acceptable: next access will be a hit or a fresh miss).
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)
        key = str(tmp_path / "repo_invalidate")

        loader_started = threading.Event()
        completed: List[str] = []
        errors: List[Exception] = []

        def slow_loader() -> Tuple[Any, Dict]:
            loader_started.set()
            time.sleep(0.2)
            return MagicMock(), {0: "vec"}

        def thread_loader():
            try:
                cache.get_or_load(key, slow_loader)
                completed.append("load")
            except Exception as e:
                errors.append(e)

        def thread_invalidator():
            loader_started.wait(timeout=2.0)
            try:
                cache.invalidate(key)
                completed.append("invalidate")
            except Exception as e:
                errors.append(e)

        ta = threading.Thread(target=thread_loader)
        tb = threading.Thread(target=thread_invalidator)
        ta.start()
        tb.start()
        # Tight timeout - deadlock would cause join to hang
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)

        assert len(errors) == 0, f"Operations raised exceptions: {errors}"
        assert "load" in completed, "Load thread must complete"
        assert "invalidate" in completed, "Invalidate thread must complete"

    def test_cleanup_during_load_no_deadlock(self, tmp_path: Path) -> None:
        """
        Background cleanup runs while a load is in progress.
        No deadlock. Cleanup only affects _cache entries, not _loading sentinels.
        The loading thread must complete successfully.
        """
        config = HNSWIndexCacheConfig(
            ttl_minutes=60.0,
            cleanup_interval_seconds=100,  # Manual cleanup only
        )
        cache = HNSWIndexCache(config=config)

        key = str(tmp_path / "repo_cleanup_during_load")
        loader_started = threading.Event()
        completed: List[str] = []
        errors: List[Exception] = []

        def slow_loader() -> Tuple[Any, Dict]:
            loader_started.set()
            time.sleep(0.2)
            return MagicMock(), {0: "vec"}

        def thread_loader():
            try:
                cache.get_or_load(key, slow_loader)
                completed.append("load")
            except Exception as e:
                errors.append(e)

        def thread_cleanup():
            loader_started.wait(timeout=2.0)
            try:
                cache._cleanup_expired_entries()
                completed.append("cleanup")
            except Exception as e:
                errors.append(e)

        ta = threading.Thread(target=thread_loader)
        tb = threading.Thread(target=thread_cleanup)
        ta.start()
        tb.start()
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)

        assert len(errors) == 0, f"Operations raised exceptions: {errors}"
        assert "load" in completed, "Load thread must complete without deadlock"
        assert "cleanup" in completed, "Cleanup thread must complete without deadlock"

    def test_multiple_waiters_all_receive_result(self, tmp_path: Path) -> None:
        """
        Many threads request the same key simultaneously.
        All waiters receive the loaded result. Loader called exactly once.
        """
        config = HNSWIndexCacheConfig(ttl_minutes=60.0)
        cache = HNSWIndexCache(config=config)

        key = str(tmp_path / "repo_many_waiters")
        mock_index = MagicMock()
        mock_index.name = "shared"
        load_count = 0
        load_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def counting_slow_loader() -> Tuple[Any, Dict]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.15)
            return mock_index, {i: f"vec_{i}" for i in range(5)}

        results: List[Optional[Tuple]] = [None] * 10
        errors: List[Optional[Exception]] = [None] * 10

        def load_thread(idx: int):
            barrier.wait()
            try:
                results[idx] = cache.get_or_load(key, counting_slow_loader)
            except Exception as e:
                errors[idx] = e

        threads = [threading.Thread(target=load_thread, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # No errors
        assert all(e is None for e in errors), f"Errors: {[e for e in errors if e]}"
        # All got results
        assert all(r is not None for r in results), "All threads must get results"
        # Loader called once
        assert load_count == 1, f"Loader called {load_count} times, expected 1"
        # All got same index
        assert all(r[0] is mock_index for r in results), "All threads must get same index"
