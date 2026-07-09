"""
Unit tests for IdIndexCache (Bug #1078 cross-query id_index caching).

Tests mirror the HNSW cache test patterns to verify correctness of:
- Cache hit returns same object (load called once)
- Cache miss calls loader
- Concurrent get_or_load for same key deduplicates (loads once)
- invalidate removes entry so next get reloads
- TTL expiry forces reload
- invalidate_prefix / clear
"""

import threading
import time
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.cache.id_index_cache import (
    IdIndexCache,
    IdIndexCacheConfig,
    get_global_id_index_cache,
    reset_global_id_index_cache,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_id_index() -> Dict[str, Any]:
    """Return a simple dict simulating an id_index."""
    return {"point_id_1": Path("/repo/file1.py"), "point_id_2": Path("/repo/file2.py")}


def _make_loader(return_value: Dict[str, Any], call_count: list):
    """Return a loader that records invocation count."""

    def loader():
        call_count.append(1)
        return return_value

    return loader


# ---------------------------------------------------------------------------
# Basic behaviour
# ---------------------------------------------------------------------------


class TestIdIndexCacheHit:
    """Cache hit returns cached object; loader called only once."""

    def test_cache_miss_calls_loader(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        key = str(tmp_path / "repo" / "coll")
        calls: list = []
        result = cache.get_or_load(key, _make_loader(_make_id_index(), calls))
        assert len(calls) == 1
        assert isinstance(result, dict)

    def test_cache_hit_returns_same_object(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        key = str(tmp_path / "repo" / "coll")
        calls: list = []
        index = _make_id_index()
        loader = _make_loader(index, calls)

        first = cache.get_or_load(key, loader)
        second = cache.get_or_load(key, loader)

        assert first is second
        assert len(calls) == 1  # loader called only once

    def test_cache_hit_does_not_call_loader_again(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        key = str(tmp_path / "repo" / "coll")
        calls: list = []
        loader = _make_loader(_make_id_index(), calls)

        cache.get_or_load(key, loader)
        cache.get_or_load(key, loader)
        cache.get_or_load(key, loader)

        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


class TestIdIndexCacheInvalidation:
    """invalidate removes entry; next get reloads."""

    def test_invalidate_forces_reload(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        key = str(tmp_path / "repo" / "coll")
        calls: list = []
        loader = _make_loader(_make_id_index(), calls)

        cache.get_or_load(key, loader)  # load once
        cache.invalidate(key)
        cache.get_or_load(key, loader)  # should reload

        assert len(calls) == 2

    def test_invalidate_returns_different_object_after_reload(
        self, tmp_path: Path
    ) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        key = str(tmp_path / "repo" / "coll")

        first_index = _make_id_index()
        second_index = _make_id_index()
        indices = [first_index, second_index]
        calls: list = []

        def loader():
            calls.append(1)
            return indices.pop(0)

        first = cache.get_or_load(key, loader)
        cache.invalidate(key)
        second = cache.get_or_load(key, loader)

        assert first is not second

    def test_invalidate_nonexistent_key_is_noop(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        # Should not raise
        cache.invalidate(str(tmp_path / "nonexistent"))

    def test_invalidate_prefix_removes_matching_entries(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        prefix = str(tmp_path / "snapshot_v1")
        key_a = str(tmp_path / "snapshot_v1" / "coll_a")
        key_b = str(tmp_path / "snapshot_v1" / "coll_b")
        key_other = str(tmp_path / "other_repo" / "coll")

        calls_a: list = []
        calls_b: list = []
        calls_other: list = []

        cache.get_or_load(key_a, _make_loader(_make_id_index(), calls_a))
        cache.get_or_load(key_b, _make_loader(_make_id_index(), calls_b))
        cache.get_or_load(key_other, _make_loader(_make_id_index(), calls_other))

        evicted = cache.invalidate_prefix(prefix)

        assert evicted == 2
        # Reload a and b (they were evicted)
        cache.get_or_load(key_a, _make_loader(_make_id_index(), calls_a))
        cache.get_or_load(key_b, _make_loader(_make_id_index(), calls_b))
        # other_repo entry still cached
        cache.get_or_load(key_other, _make_loader(_make_id_index(), calls_other))

        assert len(calls_a) == 2
        assert len(calls_b) == 2
        assert len(calls_other) == 1  # not evicted, not reloaded

    def test_invalidate_prefix_does_not_match_partial_path_name(
        self, tmp_path: Path
    ) -> None:
        """Path /a/b must NOT evict /a/barbaz — the separator guard."""
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        # Create two dirs where one name is prefix of the other
        prefix_dir = tmp_path / "snap"
        other_dir = tmp_path / "snapXYZ"  # same prefix chars but different path
        prefix_dir.mkdir()
        other_dir.mkdir()

        key_snap = str(prefix_dir / "coll")
        key_snapxyz = str(other_dir / "coll")

        calls_snap: list = []
        calls_xyz: list = []

        cache.get_or_load(key_snap, _make_loader(_make_id_index(), calls_snap))
        cache.get_or_load(key_snapxyz, _make_loader(_make_id_index(), calls_xyz))

        cache.invalidate_prefix(str(prefix_dir))

        # key_snapxyz should NOT be evicted
        cache.get_or_load(key_snapxyz, _make_loader(_make_id_index(), calls_xyz))
        assert len(calls_xyz) == 1  # still cached

    def test_clear_removes_all_entries(self, tmp_path: Path) -> None:
        cache = IdIndexCache(IdIndexCacheConfig(ttl_minutes=60.0))
        calls_a: list = []
        calls_b: list = []

        key_a = str(tmp_path / "repo_a" / "coll")
        key_b = str(tmp_path / "repo_b" / "coll")

        cache.get_or_load(key_a, _make_loader(_make_id_index(), calls_a))
        cache.get_or_load(key_b, _make_loader(_make_id_index(), calls_b))

        cache.clear()

        cache.get_or_load(key_a, _make_loader(_make_id_index(), calls_a))
        cache.get_or_load(key_b, _make_loader(_make_id_index(), calls_b))

        assert len(calls_a) == 2
        assert len(calls_b) == 2


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestIdIndexCacheTTL:
    """TTL expiry forces reload on next access."""

    def test_expired_entry_forces_reload(self, tmp_path: Path) -> None:
        # TTL 0.01 minutes = 0.6 seconds — small enough to expire quickly in test
        config = IdIndexCacheConfig(ttl_minutes=0.01)
        cache = IdIndexCache(config)
        key = str(tmp_path / "repo" / "coll")
        calls: list = []
        loader = _make_loader(_make_id_index(), calls)

        cache.get_or_load(key, loader)
        time.sleep(0.7)  # Wait past TTL
        cache.get_or_load(key, loader)

        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Concurrent deduplication (key correctness requirement)
# ---------------------------------------------------------------------------


class TestIdIndexCacheConcurrentDedup:
    """Concurrent get_or_load for same key loads only once."""

    def test_concurrent_same_key_loads_once(self, tmp_path: Path) -> None:
        """10 threads race on the same key; loader must be called exactly once."""
        config = IdIndexCacheConfig(ttl_minutes=60.0)
        cache = IdIndexCache(config)
        key = str(tmp_path / "repo" / "coll")

        load_count = [0]
        load_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def loader():
            with load_lock:
                load_count[0] += 1
            time.sleep(0.05)  # simulate I/O
            return _make_id_index()

        results = []
        errors = []

        def worker():
            barrier.wait()
            try:
                result = cache.get_or_load(key, loader)
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Errors in threads: {errors}"
        assert len(results) == 10
        assert load_count[0] == 1, f"Expected loader called once, got {load_count[0]}"

    def test_concurrent_different_keys_loads_in_parallel(self, tmp_path: Path) -> None:
        """
        Loads for different keys proceed in parallel, not serialized.
        Wall time must be < 2x single load time when both run simultaneously.
        """
        config = IdIndexCacheConfig(ttl_minutes=60.0)
        cache = IdIndexCache(config)
        key_a = str(tmp_path / "repo_a" / "coll")
        key_b = str(tmp_path / "repo_b" / "coll")
        delay = 0.3

        results = {}
        barrier = threading.Barrier(2)

        def _slow_load():
            time.sleep(delay)
            return _make_id_index()

        def load_a():
            barrier.wait()
            results["a"] = cache.get_or_load(key_a, _slow_load)

        def load_b():
            barrier.wait()
            results["b"] = cache.get_or_load(key_b, _slow_load)

        start = time.time()
        ta = threading.Thread(target=load_a)
        tb = threading.Thread(target=load_b)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)
        elapsed = time.time() - start

        assert "a" in results and "b" in results
        # Parallel: elapsed should be roughly one delay, not two
        assert elapsed < delay * 1.9, f"Loads appear to be serialized: {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


class TestIdIndexCacheSingleton:
    """get_global_id_index_cache() returns a singleton."""

    def test_singleton_returns_same_instance(self) -> None:
        reset_global_id_index_cache()
        try:
            c1 = get_global_id_index_cache()
            c2 = get_global_id_index_cache()
            assert c1 is c2
        finally:
            reset_global_id_index_cache()

    def test_reset_clears_singleton(self) -> None:
        reset_global_id_index_cache()
        try:
            c1 = get_global_id_index_cache()
            reset_global_id_index_cache()
            c2 = get_global_id_index_cache()
            assert c1 is not c2
        finally:
            reset_global_id_index_cache()
