"""
Integration test for Phase 2 of Bug #881 — Orphan Eviction on Snapshot Swap.

Simulates the RefreshScheduler alias swap scenario and verifies that
HNSWIndexCache.invalidate_prefix() correctly evicts all stale entries for the
old snapshot path while leaving the new snapshot path empty (not yet loaded).

Uses real hnswlib.Index instances and the public get_or_load() / get_stats() /
invalidate_prefix() API exclusively — no mocking, no private attribute access.

To verify new-path absence after eviction, get_or_load() is called with a loader
that records its invocation; if the loader fires, the path was not in cache (cache-miss).
"""

import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import hnswlib

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)

# Named constants — no bare numeric literals in executable code
CACHE_TTL_MINUTES = 10
CACHE_CLEANUP_INTERVAL_SECONDS = 60
HNSW_SPACE = "cosine"
HNSW_DIM = 4
HNSW_MAX_ELEMENTS = 10
HNSW_EF_CONSTRUCTION = 10
HNSW_M = 4
OLD_ENTRY_COUNT = 2  # voyage + cohere for old snapshot
ZERO_REMAINING = 0
LOADER_CALLS_EXPECTED = 2  # both new-snapshot paths must trigger loader (cache-miss)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_real_hnsw_index() -> hnswlib.Index:
    """Create a minimal real hnswlib.Index with no vectors."""
    idx = hnswlib.Index(space=HNSW_SPACE, dim=HNSW_DIM)
    idx.init_index(
        max_elements=HNSW_MAX_ELEMENTS,
        ef_construction=HNSW_EF_CONSTRUCTION,
        M=HNSW_M,
    )
    return idx


def _make_cache() -> HNSWIndexCache:
    config = HNSWIndexCacheConfig(
        ttl_minutes=CACHE_TTL_MINUTES,
        cleanup_interval_seconds=CACHE_CLEANUP_INTERVAL_SECONDS,
        max_cache_size_mb=None,
    )
    return HNSWIndexCache(config)


def _snapshot_path(base_dir: str, version: str) -> str:
    return str(Path(base_dir) / ".versioned" / "my-repo" / version)


def _collection_path(snapshot: str, provider: str) -> str:
    return str(Path(snapshot) / provider)


def _make_tracking_loader(
    call_log: List[str], key: str
) -> Callable[[], Tuple[hnswlib.Index, Dict[int, str]]]:
    """Return a loader callable that records its key in call_log when invoked.
    If get_or_load calls this loader, the path was not in cache (cache-miss).
    """

    def loader() -> Tuple[hnswlib.Index, Dict[int, str]]:
        call_log.append(key)
        return _make_real_hnsw_index(), {}

    return loader


# ---------------------------------------------------------------------------
# Integration test: alias swap evicts old entries, new path absent
# ---------------------------------------------------------------------------


def test_swap_alias_evicts_old_snapshot_entries_and_new_absent() -> None:
    """Simulate a complete alias swap cycle:

    The old snapshot has voyage and cohere entries loaded in cache from prior queries.
    After swap_alias() points to a new snapshot, invalidate_prefix(old_snapshot) is called.
    The old entries must be evicted (cache count drops to zero) and the new snapshot
    path must have no pre-loaded entries (verified by get_or_load triggering loader calls).
    """
    with tempfile.TemporaryDirectory() as base:
        cache = _make_cache()

        old_snapshot = _snapshot_path(base, "v_20240101_120000")
        new_snapshot = _snapshot_path(base, "v_20240102_120000")

        old_voyage = _collection_path(old_snapshot, "voyage-three")
        old_cohere = _collection_path(old_snapshot, "cohere")

        # Populate via public API — same path used by the server at query time
        cache.get_or_load(old_voyage, lambda: (_make_real_hnsw_index(), {}))
        cache.get_or_load(old_cohere, lambda: (_make_real_hnsw_index(), {}))

        stats_before = cache.get_stats()
        assert stats_before.cached_repositories == OLD_ENTRY_COUNT, (
            f"Setup: expected {OLD_ENTRY_COUNT} cached entries, "
            f"got {stats_before.cached_repositories}"
        )

        # Simulate what RefreshScheduler does after swap_alias()
        evicted = cache.invalidate_prefix(old_snapshot)

        assert evicted == OLD_ENTRY_COUNT, (
            f"Expected {OLD_ENTRY_COUNT} evictions after swap, got {evicted}"
        )

        stats_after = cache.get_stats()
        assert stats_after.cached_repositories == ZERO_REMAINING, (
            f"Cache must be empty after eviction, "
            f"got {stats_after.cached_repositories} entries"
        )

        # Verify new snapshot entries are absent: get_or_load must invoke the loader
        # (a cache hit would skip the loader entirely)
        new_voyage = _collection_path(new_snapshot, "voyage-three")
        new_cohere = _collection_path(new_snapshot, "cohere")
        loader_calls: List[str] = []

        cache.get_or_load(new_voyage, _make_tracking_loader(loader_calls, new_voyage))
        cache.get_or_load(new_cohere, _make_tracking_loader(loader_calls, new_cohere))

        assert len(loader_calls) == LOADER_CALLS_EXPECTED, (
            f"Expected {LOADER_CALLS_EXPECTED} loader calls (cache-miss for new paths), "
            f"got {len(loader_calls)}: {loader_calls}"
        )
