"""Story 4 — Part 6 (revised): YELLOW proactive LRU eviction via evict_lru_to_floor().

Tests against the REAL HNSWIndexCache API (not mocks) to avoid silent no-op failures.
HNSWIndexCacheStats is a DATACLASS — .cached_repositories (int), not a subscriptable
dict.  evict_lru_entries(n) is the real method added to HNSWIndexCache in Story 4.

Tests:
- Method exists, lru_evictions incremented by evicted count, trim_calls incremented.
- Floor respected when cache is at or below floor (no eviction).
- Evicts exactly (size - floor) entries using the real cache API.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_100_GIB = 100 * BYTES_PER_GIB
PERCENT_DENOMINATOR = 100
NO_SWAP_PAGES_IN = 0
NO_RED_DWELL_SECONDS = 0.0

GREEN_USAGE_PCT = 30.0
YELLOW_PCT_DEFAULT = 70.0
RED_PCT_DEFAULT = 85.0
HYSTERESIS_PCT_DEFAULT = 10.0

# Number of fake entries to pre-populate in the real cache
CACHE_ENTRIES_ABOVE_FLOOR = 5
CACHE_FLOOR = 3  # evict 5-3=2
EVICT_COUNT_ABOVE_FLOOR = CACHE_ENTRIES_ABOVE_FLOOR - CACHE_FLOOR  # 2

CACHE_ENTRIES_AT_FLOOR = 3  # exactly at floor → no eviction
CACHE_ENTRIES_BELOW_FLOOR = 2  # below floor → no eviction

CACHE_ENTRIES_LARGE = 7
LARGE_FLOOR = 2
LARGE_EVICT_COUNT = CACHE_ENTRIES_LARGE - LARGE_FLOOR  # 5

# TTL long enough that entries don't expire during the test
LONG_TTL_MINUTES = 60.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_100_GIB
    vm.used = int(HOST_100_GIB * used_pct / PERCENT_DENOMINATOR)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = NO_SWAP_PAGES_IN
    return readers


def _green_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(GREEN_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT_DEFAULT,
        red_pct=RED_PCT_DEFAULT,
        hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.GREEN
    return gov


def _real_cache(entry_count: int) -> HNSWIndexCache:
    """Return a real HNSWIndexCache pre-populated with `entry_count` fake entries."""
    cfg = HNSWIndexCacheConfig(ttl_minutes=LONG_TTL_MINUTES)
    cache = HNSWIndexCache(config=cfg)
    for i in range(entry_count):
        fake_index = MagicMock()
        fake_index.index_file_size.return_value = 0
        # Inject directly into internal dict to avoid calling the real loader
        from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCacheEntry

        entry = HNSWIndexCacheEntry(
            hnsw_index=fake_index,
            id_mapping={},
            repo_path=f"/fake/repo/{i}",
            ttl_minutes=LONG_TTL_MINUTES,
        )
        cache._cache[f"/fake/repo/{i}"] = entry
    return cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestYellowProactiveLruEviction:
    """evict_lru_to_floor() uses the REAL HNSWIndexCache API."""

    def test_method_exists_and_counters_incremented_with_real_cache(self):
        """evict_lru_to_floor exists; lru_evictions incremented by evicted count;
        trim_calls incremented by at least 1 — verified against real HNSWIndexCache."""
        gov = _green_gov()
        cache = _real_cache(CACHE_ENTRIES_ABOVE_FLOOR)

        # Verify real get_stats() is a dataclass not a dict
        stats = cache.get_stats()
        assert stats.cached_repositories == CACHE_ENTRIES_ABOVE_FLOOR

        assert hasattr(gov, "evict_lru_to_floor"), "Missing evict_lru_to_floor()"
        before_lru = gov.counters.lru_evictions
        before_trim = gov.counters.trim_calls

        gov.evict_lru_to_floor(cache, floor_entries=CACHE_FLOOR)

        assert gov.counters.lru_evictions == before_lru + EVICT_COUNT_ABOVE_FLOOR
        assert gov.counters.trim_calls >= before_trim + 1
        assert cache.get_stats().cached_repositories == CACHE_FLOOR

    def test_floor_respected_with_real_cache(self):
        """No eviction when real cache size is at or below floor."""
        gov = _green_gov()

        cache_at = _real_cache(CACHE_ENTRIES_AT_FLOOR)
        gov.evict_lru_to_floor(cache_at, floor_entries=CACHE_FLOOR)
        assert cache_at.get_stats().cached_repositories == CACHE_ENTRIES_AT_FLOOR

        cache_below = _real_cache(CACHE_ENTRIES_BELOW_FLOOR)
        gov.evict_lru_to_floor(cache_below, floor_entries=CACHE_FLOOR)
        assert cache_below.get_stats().cached_repositories == CACHE_ENTRIES_BELOW_FLOOR

    def test_evicts_exact_count_with_real_cache(self):
        """evict_lru_to_floor evicts exactly (size - floor_entries) using real cache."""
        gov = _green_gov()
        cache = _real_cache(CACHE_ENTRIES_LARGE)

        gov.evict_lru_to_floor(cache, floor_entries=LARGE_FLOOR)

        remaining = cache.get_stats().cached_repositories
        assert remaining == LARGE_FLOOR, (
            f"Expected {LARGE_FLOOR} entries remaining after eviction, got {remaining}"
        )
