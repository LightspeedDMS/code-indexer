"""
Tests for HNSWIndexCache.invalidate_prefix() — Phase 2 of Bug #881.

Mechanism A fix: RefreshScheduler.swap_alias() creates new snapshot paths;
invalidate_prefix() evicts all stale entries at the OLD snapshot path prefix
immediately, rather than waiting for TTL.

The test suite covers an empty cache returning zero evictions, a cache with
no matching keys returning zero evictions and leaving the cache unchanged,
an exact key match evicting exactly one entry, prefix matching with a separator
guard so that /a/b evicts /a/b/coll but not /a/barbaz, mixed matching where only
the matching keys are evicted, thread-safety when two threads call invalidate_prefix
concurrently, and correct increment of the internal eviction_count counter.
"""

import queue
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Named constants — all numeric literals in this file are constant definitions;
# no bare numeric literals appear in executable assertions or expressions.
# ---------------------------------------------------------------------------
CACHE_TTL_MINUTES = 10
CACHE_CLEANUP_INTERVAL_SECONDS = 60
STUB_ENTRY_SIZE_BYTES = 1024
INITIAL_ACCESS_COUNT = 1
ZERO_EVICTIONS = 0
SINGLE_EVICTION = 1
EMPTY_CACHE_SIZE = 0
SINGLE_REMAINING_ENTRY = 1
ENTRIES_PER_THREAD = 50
THREAD_COUNT = 2
BARRIER_TIMEOUT_SECONDS = 5
TIMEOUT_INCREMENT_SECONDS = 1
JOIN_TIMEOUT_SECONDS = BARRIER_TIMEOUT_SECONDS + TIMEOUT_INCREMENT_SECONDS
EVICTION_ENTRIES = 5
UNRELATED_KEYS_COUNT = 2
OLD_SNAPSHOT_PROVIDER_COUNT = 2
NEW_SNAPSHOT_PROVIDER_COUNT = 2
SEP_GUARD_EVICT_COUNT = 3
SEP_GUARD_KEEP_COUNT = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_entry(repo_path: str) -> Any:
    """Return a minimal HNSWIndexCacheEntry-compatible stub (no hnswlib I/O)."""
    from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCacheEntry

    entry = MagicMock(spec=HNSWIndexCacheEntry)
    entry.repo_path = repo_path
    entry.index_size_bytes = STUB_ENTRY_SIZE_BYTES
    entry.last_accessed = datetime.now()
    entry.is_expired.return_value = False
    entry.access_count = INITIAL_ACCESS_COUNT
    return entry


def _make_cache() -> Any:
    """Return a fresh HNSWIndexCache with default test configuration."""
    from code_indexer.server.cache.hnsw_index_cache import (
        HNSWIndexCache,
        HNSWIndexCacheConfig,
    )

    config = HNSWIndexCacheConfig(
        ttl_minutes=CACHE_TTL_MINUTES,
        cleanup_interval_seconds=CACHE_CLEANUP_INTERVAL_SECONDS,
        max_cache_size_mb=None,
    )
    return HNSWIndexCache(config)


def _populate(cache: Any, keys: list) -> None:
    """Directly insert stub entries for the given keys, bypassing the loader."""
    with cache._cache_lock:
        for key in keys:
            resolved = str(Path(key).resolve())
            cache._cache[resolved] = _make_stub_entry(resolved)


def _snapshot_path(base_dir: str, alias: str, version: str) -> str:
    """Build a neutral versioned snapshot path under a temp base directory."""
    return str(Path(base_dir) / "versioned" / alias / version)


def _collection_path(snapshot: str, provider: str) -> str:
    return str(Path(snapshot) / provider)


# ---------------------------------------------------------------------------
# Test: empty cache returns zero evictions
# ---------------------------------------------------------------------------


def test_invalidate_prefix_on_empty_cache_returns_zero():
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        prefix = _snapshot_path(base, "repo-x", "v_old")
        result = cache.invalidate_prefix(prefix)
    assert result == ZERO_EVICTIONS


# ---------------------------------------------------------------------------
# Test: no matching keys returns zero evictions, cache unchanged
# ---------------------------------------------------------------------------


def test_invalidate_prefix_no_match_returns_zero_and_cache_unchanged():
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        other_snap = _snapshot_path(base, "other-repo", "v_current")
        unrelated_keys = [
            _collection_path(other_snap, "voyage"),
            _collection_path(other_snap, "cohere"),
        ]
        _populate(cache, unrelated_keys)
        different_prefix = _snapshot_path(base, "completely-different", "v_old")

        result = cache.invalidate_prefix(different_prefix)

    assert result == ZERO_EVICTIONS
    assert len(cache._cache) == UNRELATED_KEYS_COUNT


# ---------------------------------------------------------------------------
# Test: exact match evicts exactly one entry
# ---------------------------------------------------------------------------


def test_invalidate_prefix_exact_match_evicts_one():
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        old_snap = _snapshot_path(base, "repo-y", "v_old")
        new_snap = _snapshot_path(base, "repo-y", "v_new")
        new_entry = _collection_path(new_snap, "voyage")

        _populate(cache, [old_snap, new_entry])
        result = cache.invalidate_prefix(old_snap)

    assert result == SINGLE_EVICTION
    resolved_old = str(Path(old_snap).resolve())
    assert resolved_old not in cache._cache
    assert len(cache._cache) == SINGLE_REMAINING_ENTRY


# ---------------------------------------------------------------------------
# Test: separator guard prevents over-eviction of sibling paths
# ---------------------------------------------------------------------------


def test_invalidate_prefix_separator_guard_no_over_eviction():
    """Paths sharing a textual prefix but differing by a full path component
    must NOT be evicted. Using prefix /repo/b: the exact path /repo/b and its
    children /repo/b/coll-a and /repo/b/coll-b are evicted, while /repo/barbaz/coll
    (shares /repo/b text but is a different path component) and /repo/c/d
    (unrelated subtree) are preserved.
    """
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        repo_root = str(Path(base) / "repo")
        prefix = str(Path(repo_root) / "b")

        keys_to_evict = [
            prefix,
            str(Path(prefix) / "coll-a"),
            str(Path(prefix) / "coll-b"),
        ]
        keys_to_keep = [
            str(Path(repo_root) / "barbaz" / "coll"),
            str(Path(repo_root) / "c" / "d"),
        ]
        _populate(cache, keys_to_evict + keys_to_keep)

        result = cache.invalidate_prefix(prefix)

    assert result == SEP_GUARD_EVICT_COUNT, (
        f"Expected {SEP_GUARD_EVICT_COUNT} evictions, got {result}"
    )
    for key in keys_to_evict:
        assert str(Path(key).resolve()) not in cache._cache, (
            f"Key {key!r} should have been evicted"
        )
    assert len(cache._cache) == SEP_GUARD_KEEP_COUNT, (
        f"Expected {SEP_GUARD_KEEP_COUNT} entries remaining, got {len(cache._cache)}"
    )


# ---------------------------------------------------------------------------
# Test: mixed match + non-match — only old snapshot entries evicted
# ---------------------------------------------------------------------------


def test_invalidate_prefix_mixed_evicts_only_matching():
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        old_snap = _snapshot_path(base, "repo-z", "v_old")
        new_snap = _snapshot_path(base, "repo-z", "v_new")
        old_keys = [
            _collection_path(old_snap, "voyage"),
            _collection_path(old_snap, "cohere"),
        ]
        new_keys = [
            _collection_path(new_snap, "voyage"),
            _collection_path(new_snap, "cohere"),
        ]
        _populate(cache, old_keys + new_keys)

        result = cache.invalidate_prefix(old_snap)

    assert result == OLD_SNAPSHOT_PROVIDER_COUNT
    for key in old_keys:
        assert str(Path(key).resolve()) not in cache._cache
    assert len(cache._cache) == NEW_SNAPSHOT_PROVIDER_COUNT


# ---------------------------------------------------------------------------
# Test: thread-safety — two concurrent invalidate_prefix calls
# ---------------------------------------------------------------------------


def test_invalidate_prefix_thread_safety():
    """Two threads evicting different prefixes simultaneously must not corrupt
    cache state. Results collected via thread-safe queues (no shared dicts).
    """
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        prefix_a = _snapshot_path(base, "repo-a", "v_old")
        prefix_b = _snapshot_path(base, "repo-b", "v_old")

        keys_a = [
            _collection_path(prefix_a, f"coll-{i}") for i in range(ENTRIES_PER_THREAD)
        ]
        keys_b = [
            _collection_path(prefix_b, f"coll-{i}") for i in range(ENTRIES_PER_THREAD)
        ]
        _populate(cache, keys_a + keys_b)

        result_queue: queue.Queue = queue.Queue()
        error_queue: queue.Queue = queue.Queue()
        barrier = threading.Barrier(THREAD_COUNT, timeout=BARRIER_TIMEOUT_SECONDS)

        def evict(prefix: str, label: str) -> None:
            try:
                barrier.wait()
                count = cache.invalidate_prefix(prefix)
                result_queue.put((label, count))
            except Exception as exc:
                error_queue.put((label, exc))

        threads = [
            threading.Thread(target=evict, args=(prefix_a, "thread-a")),
            threading.Thread(target=evict, args=(prefix_b, "thread-b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=JOIN_TIMEOUT_SECONDS)

    errors = list(error_queue.queue)
    assert not errors, f"Thread errors: {errors}"

    results = dict(result_queue.queue)
    assert results.get("thread-a") == ENTRIES_PER_THREAD, (
        f"thread-a evicted {results.get('thread-a')}, expected {ENTRIES_PER_THREAD}"
    )
    assert results.get("thread-b") == ENTRIES_PER_THREAD, (
        f"thread-b evicted {results.get('thread-b')}, expected {ENTRIES_PER_THREAD}"
    )
    assert len(cache._cache) == EMPTY_CACHE_SIZE, (
        f"Cache should be empty after both prefixes evicted, got {len(cache._cache)} entries"
    )


# ---------------------------------------------------------------------------
# Test: _eviction_count incremented for each evicted entry
# ---------------------------------------------------------------------------


def test_invalidate_prefix_increments_eviction_count():
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()
        prefix = _snapshot_path(base, "repo-w", "v_old")
        keys = [_collection_path(prefix, f"coll-{i}") for i in range(EVICTION_ENTRIES)]
        _populate(cache, keys)

        initial_count = cache._eviction_count
        result = cache.invalidate_prefix(prefix)

    assert result == EVICTION_ENTRIES
    assert cache._eviction_count == initial_count + EVICTION_ENTRIES, (
        f"_eviction_count should increase by {EVICTION_ENTRIES}: "
        f"was {initial_count}, now {cache._eviction_count}"
    )
