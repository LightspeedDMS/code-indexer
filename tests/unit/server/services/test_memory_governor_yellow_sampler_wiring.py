"""Story 4 Critical 2 — YELLOW sampler tick calls evict_lru_to_floor on attached cache.

Tests:
1. attach_cache() method exists and stores the cache reference.
2. When band is YELLOW and a cache is attached, _tick() calls evict_lru_to_floor().
3. When no cache is attached, _tick() does NOT call evict_lru_to_floor() (None-guard).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_8_GIB = 8 * BYTES_PER_GIB
PERCENT_DENOMINATOR = 100
NO_SWAP_PAGES_IN = 0
NO_RED_DWELL_SECONDS = 0.0

YELLOW_USAGE_PCT = 72.0  # inside [yellow=70, red_exit=75) so band stays YELLOW
YELLOW_PCT = 70.0
RED_PCT = 85.0
HYSTERESIS_PCT = 10.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_8_GIB
    vm.used = int(HOST_8_GIB * used_pct / PERCENT_DENOMINATOR)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = NO_SWAP_PAGES_IN
    return readers


def _yellow_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(YELLOW_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT,
        red_pct=RED_PCT,
        hysteresis_pct=HYSTERESIS_PCT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.YELLOW, f"Expected YELLOW, got {gov.band}"
    return gov


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestYellowSamplerCacheWiring:
    """YELLOW sampler tick must call evict_lru_to_floor on the attached cache."""

    def test_attach_cache_stores_reference(self):
        """attach_cache() must exist and store the cache so _tick() can call it."""
        gov = _yellow_gov()
        assert hasattr(gov, "attach_cache"), "MemoryGovernor missing attach_cache()"
        assert callable(gov.attach_cache)

        cache = MagicMock()
        gov.attach_cache(cache)
        # The cache should be accessible (stored as _cache or similar)
        assert gov._attached_cache is cache

    def test_tick_calls_evict_lru_when_yellow_and_cache_attached(self):
        """When YELLOW and cache attached, _tick() must call evict_lru_to_floor().

        This is the CALL-SITE test: we spy on evict_lru_to_floor to confirm
        _tick() reaches it when band is YELLOW.
        """
        gov = _yellow_gov()
        cache = MagicMock()
        gov.attach_cache(cache)

        call_count = [0]
        original = gov.evict_lru_to_floor

        def _spy(c, *, floor_entries):
            call_count[0] += 1
            original(c, floor_entries=floor_entries)

        gov.evict_lru_to_floor = _spy  # type: ignore[method-assign]

        gov._tick()

        assert call_count[0] >= 1, (
            "evict_lru_to_floor must be called from _tick() when band is YELLOW "
            "and a cache is attached."
        )

    def test_tick_does_not_call_evict_when_no_cache(self):
        """When no cache is attached, _tick() must NOT call evict_lru_to_floor()."""
        gov = _yellow_gov()
        # No attach_cache() call

        call_count = [0]
        original = gov.evict_lru_to_floor

        def _spy(c, *, floor_entries):
            call_count[0] += 1
            original(c, floor_entries=floor_entries)

        gov.evict_lru_to_floor = _spy  # type: ignore[method-assign]

        gov._tick()

        assert call_count[0] == 0, (
            "evict_lru_to_floor must NOT be called when no cache is attached."
        )
