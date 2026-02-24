"""
Concurrent tests for FTSIndexCache per-key Event sentinel pattern.

Story #277: Non-Blocking HNSW/FTS Cache Population for Concurrent Multi-Repo Search

Verifies that get_or_load() releases the global lock during disk I/O, enabling:
- Parallel cache population for different keys (not serialized)
- Load deduplication for same key (only one thread calls loader)
- Proper failure propagation and sentinel cleanup
- No deadlocks under concurrent load/invalidate/cleanup

These tests are written FIRST (TDD RED phase). They will FAIL against the
current implementation that holds the global lock during the loader() call,
because that serializes all concurrent loads regardless of key.

FTS-specific notes:
- Loader returns (tantivy_index, schema) tuples
- reload_on_access behavior is on the cache-HIT path only — not affected by sentinel pattern
"""

import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.cache.fts_index_cache import (
    FTSIndexCache,
    FTSIndexCacheConfig,
)


class TestFTSCacheParallelLoads:
    """Tests verifying that loads for different keys run in parallel (not serialized)."""

    def _make_slow_fts_loader(
        self,
        delay: float,
        index_name: str = "mock_fts_index",
    ):
        """Create a slow FTS loader that simulates disk I/O."""

        def loader() -> Tuple[Any, Any]:
            time.sleep(delay)
            mock_index = MagicMock()
            mock_index.name = index_name
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()
            mock_schema.name = f"{index_name}_schema"
            return mock_index, mock_schema

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
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key_a = str(tmp_path / "fts_a")
        key_b = str(tmp_path / "fts_b")
        load_delay = 0.3  # 300ms per load - clear signal for serialization vs parallel

        results: List[Optional[Tuple[Any, Any]]] = [None, None]
        errors: List[Optional[Exception]] = [None, None]

        # Barrier ensures both threads start loading at exactly the same time
        barrier = threading.Barrier(2)

        def load_a():
            barrier.wait()
            try:
                results[0] = cache.get_or_load(key_a, self._make_slow_fts_loader(load_delay, "fts_a"))
            except Exception as e:
                errors[0] = e

        def load_b():
            barrier.wait()
            try:
                results[1] = cache.get_or_load(key_b, self._make_slow_fts_loader(load_delay, "fts_b"))
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
            f"FTS loads were serialized: wall_time={wall_time:.3f}s, "
            f"expected < {max_allowed_time:.3f}s (2 parallel loads of {load_delay}s each)"
        )

    def test_deduplicated_load_same_key(self, tmp_path: Path) -> None:
        """
        Two threads request the same FTS key. Only one thread calls loader().
        The second thread waits and receives the result from the first thread's load.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_shared")
        load_count = 0
        load_lock = threading.Lock()

        def counting_loader() -> Tuple[Any, Any]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.2)  # Slow enough that both threads start before one finishes
            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            mock_schema = MagicMock()
            return mock_index, mock_schema

        results: List[Optional[Tuple[Any, Any]]] = [None, None]
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
        index_0, schema_0 = results[0]
        index_1, schema_1 = results[1]
        assert index_0 is index_1, "Both threads must receive the same tantivy index"
        assert schema_0 is schema_1, "Both threads must receive the same schema"

    def test_waiter_receives_loaded_result(self, tmp_path: Path) -> None:
        """
        First thread loads FTS index, second thread waits on the Event.
        Both receive identical results (same objects, not copies).
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_waited")
        mock_index = MagicMock()
        mock_index.name = "waited_fts"
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()

        loader_started = threading.Event()

        def slow_loader() -> Tuple[Any, Any]:
            loader_started.set()
            time.sleep(0.2)
            return mock_index, mock_schema

        results: List[Optional[Tuple[Any, Any]]] = [None, None]

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

        index_a, schema_a = results[0]
        index_b, schema_b = results[1]

        assert index_a is mock_index, "Thread A must get the mock FTS index"
        assert index_b is mock_index, "Thread B must get the same mock FTS index"
        assert index_a is index_b, "Both threads must see the same index object"
        assert schema_a is mock_schema, "Thread A must get the mock schema"
        assert schema_b is mock_schema, "Thread B must get the same mock schema"
        assert schema_a is schema_b, "Both threads must see the same schema"

    def test_cache_hit_not_blocked_by_loading(self, tmp_path: Path) -> None:
        """
        Thread A is loading key-B (slow FTS load). Thread C requests already-cached key-A.
        Thread C must return from cache immediately without waiting for thread A.

        With the old global lock pattern, thread C would block while thread A
        holds the lock during I/O. With the sentinel pattern, thread C only needs
        the lock for the fast dict lookup.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key_cached = str(tmp_path / "fts_cached")
        key_loading = str(tmp_path / "fts_loading")

        # Pre-populate key_cached
        mock_cached_index = MagicMock()
        mock_cached_index.name = "cached_fts"
        mock_cached_index.reload = MagicMock()
        mock_cached_schema = MagicMock()
        cache.get_or_load(key_cached, lambda: (mock_cached_index, mock_cached_schema))

        load_delay = 0.4  # 400ms - clearly detectable if thread C has to wait
        loader_started = threading.Event()

        def slow_loader_for_b() -> Tuple[Any, Any]:
            loader_started.set()
            time.sleep(load_delay)
            new_index = MagicMock()
            new_index.reload = MagicMock()
            return new_index, MagicMock()

        hit_duration: List[float] = []

        def thread_loading_b():
            cache.get_or_load(key_loading, slow_loader_for_b)

        def thread_cache_hit_c():
            loader_started.wait(timeout=2.0)
            start = time.monotonic()
            result_index, result_schema = cache.get_or_load(
                key_cached, lambda: (MagicMock(), MagicMock())
            )
            duration = time.monotonic() - start
            hit_duration.append(duration)
            assert result_index is mock_cached_index

        ta = threading.Thread(target=thread_loading_b)
        tc = threading.Thread(target=thread_cache_hit_c)

        ta.start()
        tc.start()
        ta.join(timeout=5.0)
        tc.join(timeout=5.0)

        assert len(hit_duration) == 1, "Thread C did not complete"
        max_hit_duration = load_delay * 0.25
        assert hit_duration[0] < max_hit_duration, (
            f"FTS cache hit was blocked by ongoing load: "
            f"hit_duration={hit_duration[0]:.3f}s, expected < {max_hit_duration:.3f}s"
        )


class TestFTSCacheLoaderFailure:
    """Tests verifying correct sentinel cleanup and failure propagation on loader errors."""

    def test_loader_failure_signals_event(self, tmp_path: Path) -> None:
        """
        When FTS loader raises IOError, the Event must be signaled (so waiters wake up),
        the sentinel must be removed from _loading, and the exception must propagate.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)
        key = str(tmp_path / "fts_fail")

        def failing_loader() -> Tuple[Any, Any]:
            time.sleep(0.05)
            raise IOError("FTS index read failed")

        with pytest.raises(IOError, match="FTS index read failed"):
            cache.get_or_load(key, failing_loader)

        # Sentinel must be removed after failure
        normalized_key = str(Path(key).resolve())
        loading_dict = getattr(cache, "_loading", {})
        assert normalized_key not in loading_dict, (
            f"Stale FTS sentinel found in _loading after failure"
        )

        # Cache must NOT contain a stale entry
        assert normalized_key not in cache._cache, "Failed load must not leave a FTS cache entry"

    def test_loader_failure_waiter_retries(self, tmp_path: Path) -> None:
        """
        Thread-1 loads FTS index and fails. Thread-2 is waiting on the Event.
        When thread-1 fails, thread-2 wakes up, becomes the new loader, and succeeds.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)
        key = str(tmp_path / "fts_retry")

        call_count = 0
        call_lock = threading.Lock()
        loader_1_started = threading.Event()
        mock_index = MagicMock()
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()

        def loader_that_fails_first_time() -> Tuple[Any, Any]:
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count

            if current_call == 1:
                loader_1_started.set()
                time.sleep(0.1)
                raise IOError("First FTS load failed")
            else:
                time.sleep(0.05)
                return mock_index, mock_schema

        results_errors: List[Optional[Exception]] = [None, None]
        results_values: List[Optional[Tuple]] = [None, None]

        def thread_1():
            try:
                results_values[0] = cache.get_or_load(key, loader_that_fails_first_time)
            except Exception as e:
                results_errors[0] = e

        def thread_2():
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

        assert results_errors[0] is not None, "Thread-1 should have received the IOError"
        assert isinstance(results_errors[0], IOError)
        assert results_errors[1] is None, f"Thread-2 should not have raised: {results_errors[1]}"
        assert results_values[1] is not None, "Thread-2 should have gotten a FTS result"
        result_index, result_schema = results_values[1]
        assert result_index is mock_index

    def test_loader_failure_no_stale_sentinel(self, tmp_path: Path) -> None:
        """
        After a FTS loader failure, _loading dict must not contain the key.
        Subsequent requests must go through the normal 'first loader' path.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)
        key = str(tmp_path / "fts_no_stale")
        normalized_key = str(Path(key).resolve())

        def failing_loader() -> Tuple[Any, Any]:
            raise RuntimeError("FTS load failed")

        with pytest.raises(RuntimeError):
            cache.get_or_load(key, failing_loader)

        # Verify no stale sentinel
        loading_dict = getattr(cache, "_loading", {})
        assert normalized_key not in loading_dict, (
            f"Stale FTS sentinel found in _loading after failure: {loading_dict}"
        )

        # Second attempt must succeed
        mock_index = MagicMock()
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()
        load_count = 0

        def successful_loader() -> Tuple[Any, Any]:
            nonlocal load_count
            load_count += 1
            return mock_index, mock_schema

        result = cache.get_or_load(key, successful_loader)
        assert result[0] is mock_index, "Second FTS attempt must succeed"
        assert load_count == 1, "Second attempt must call loader once"
        assert normalized_key in cache._cache


class TestFTSCacheEdgeCases:
    """Edge case tests for concurrent FTS cache operations."""

    def test_expired_entry_with_concurrent_load(self, tmp_path: Path) -> None:
        """
        FTS entry exists but is expired. Two threads hit simultaneously.
        Only one calls loader(); the other waits. No duplicate loads.
        """
        config = FTSIndexCacheConfig(ttl_minutes=0.0001, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_expired")
        mock_index = MagicMock()
        mock_index.reload = MagicMock()

        # Seed with entry that will expire
        stale_index = MagicMock()
        stale_index.reload = MagicMock()
        cache.get_or_load(key, lambda: (stale_index, MagicMock()))

        # Wait for expiry
        time.sleep(0.05)

        load_count = 0
        load_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def counting_loader() -> Tuple[Any, Any]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.1)
            return mock_index, MagicMock()

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
        assert load_count == 1, (
            f"Expected exactly 1 FTS loader call for expired entry with 2 concurrent threads, "
            f"got {load_count}"
        )
        assert results[0][0] is mock_index
        assert results[1][0] is mock_index

    def test_invalidate_during_load_no_deadlock(self, tmp_path: Path) -> None:
        """
        Thread A is loading FTS key. Thread B calls invalidate(key) concurrently.
        No deadlock must occur.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)
        key = str(tmp_path / "fts_invalidate")

        loader_started = threading.Event()
        completed: List[str] = []
        errors: List[Exception] = []

        def slow_loader() -> Tuple[Any, Any]:
            loader_started.set()
            time.sleep(0.2)
            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            return mock_index, MagicMock()

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
        ta.join(timeout=3.0)
        tb.join(timeout=3.0)

        assert len(errors) == 0, f"FTS operations raised exceptions: {errors}"
        assert "load" in completed, "FTS load thread must complete"
        assert "invalidate" in completed, "FTS invalidate thread must complete"

    def test_cleanup_during_load_no_deadlock(self, tmp_path: Path) -> None:
        """
        Background cleanup runs while a FTS load is in progress.
        No deadlock. The loading thread must complete successfully.
        """
        config = FTSIndexCacheConfig(
            ttl_minutes=60.0,
            cleanup_interval_seconds=100,
            reload_on_access=False,
        )
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_cleanup_during_load")
        loader_started = threading.Event()
        completed: List[str] = []
        errors: List[Exception] = []

        def slow_loader() -> Tuple[Any, Any]:
            loader_started.set()
            time.sleep(0.2)
            mock_index = MagicMock()
            mock_index.reload = MagicMock()
            return mock_index, MagicMock()

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

        assert len(errors) == 0, f"FTS operations raised exceptions: {errors}"
        assert "load" in completed, "FTS load thread must complete without deadlock"
        assert "cleanup" in completed, "FTS cleanup thread must complete without deadlock"

    def test_reload_on_access_not_affected_by_sentinel_pattern(self, tmp_path: Path) -> None:
        """
        The reload_on_access behavior (cache HIT path) must remain unchanged.
        This is explicitly out of scope for the sentinel pattern.
        Verify that reload() is still called on cache hits when reload_on_access=True.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=True)
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_reload_on_access")
        mock_index = MagicMock()
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()

        # Initial load (cache miss - no reload)
        cache.get_or_load(key, lambda: (mock_index, mock_schema))
        mock_index.reload.assert_not_called()

        # Cache hit - reload_on_access=True should call reload()
        cache.get_or_load(key, lambda: (MagicMock(), MagicMock()))
        mock_index.reload.assert_called_once()

        # Second cache hit - reload called again
        cache.get_or_load(key, lambda: (MagicMock(), MagicMock()))
        assert mock_index.reload.call_count == 2

    def test_multiple_waiters_all_receive_result(self, tmp_path: Path) -> None:
        """
        Many threads request the same FTS key simultaneously.
        All waiters receive the loaded result. Loader called exactly once.
        """
        config = FTSIndexCacheConfig(ttl_minutes=60.0, reload_on_access=False)
        cache = FTSIndexCache(config=config)

        key = str(tmp_path / "fts_many_waiters")
        mock_index = MagicMock()
        mock_index.name = "shared_fts"
        mock_index.reload = MagicMock()
        mock_schema = MagicMock()
        load_count = 0
        load_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def counting_slow_loader() -> Tuple[Any, Any]:
            nonlocal load_count
            with load_lock:
                load_count += 1
            time.sleep(0.15)
            return mock_index, mock_schema

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

        assert all(e is None for e in errors), f"FTS errors: {[e for e in errors if e]}"
        assert all(r is not None for r in results), "All FTS threads must get results"
        assert load_count == 1, f"FTS loader called {load_count} times, expected 1"
        assert all(r[0] is mock_index for r in results), "All threads must get same FTS index"
        assert all(r[1] is mock_schema for r in results), "All threads must get same schema"
