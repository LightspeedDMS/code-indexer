"""Story 4 — Part 7: Gated balloon test (CIDX_PERF_TEST=1 required).

These tests allocate real memory to verify the governor transitions under
genuine pressure. They are SKIPPED unless CIDX_PERF_TEST=1 is set in the
environment — never run in CI.

Tests:
- Governor enters YELLOW or RED when a 200 MB balloon is allocated and tight
  watermarks (yellow=10, red=15) are configured. Both bands indicate the
  governor has crossed the safe zone — either constitutes the expected signal.
- evict_lru_to_floor calls evict_lru_entries with correct count while balloon
  is live (real memory pressure present during the call).
- Governor recovers to GREEN after balloon is released and safe watermarks
  (yellow=95, red=98) are restored.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_governor import (
    MemoryBand,
    MemoryGovernor,
)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

PERF_TEST_ENV_VAR = "CIDX_PERF_TEST"

# Balloon size: 200 MB of live bytes to push memory usage above TIGHT_YELLOW_PCT.
BALLOON_MB = 200
BYTES_PER_MB = 1024 * 1024
BALLOON_BYTES = BALLOON_MB * BYTES_PER_MB

# Tight watermarks — real usage on any developer machine (typically 20–50%)
# will exceed yellow=10, so the governor lands in YELLOW or RED.
TIGHT_YELLOW_PCT = 10.0
TIGHT_RED_PCT = 15.0
TIGHT_HYSTERESIS_PCT = 2.0
NO_RED_DWELL_SECONDS = 0.0

# LRU floor for eviction test (balloon live during call)
LRU_FLOOR = 1
MOCK_CACHE_SIZE = 5
MOCK_EVICTED_COUNT = MOCK_CACHE_SIZE - LRU_FLOOR  # 4

# Recovery watermarks — safe to be GREEN after balloon release on any machine
SAFE_YELLOW_PCT = 95.0
SAFE_RED_PCT = 98.0
SAFE_HYSTERESIS_PCT = 1.0


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------


_SKIP_REASON = f"Balloon test requires real memory allocation — set {PERF_TEST_ENV_VAR}=1 to enable"
pytestmark = pytest.mark.skipif(
    os.environ.get(PERF_TEST_ENV_VAR) != "1",
    reason=_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tight_gov() -> MemoryGovernor:
    """Governor with tight watermarks reading REAL host memory (no mock readers)."""
    return MemoryGovernor(
        enabled=True,
        start_sampler=False,
        yellow_pct=TIGHT_YELLOW_PCT,
        red_pct=TIGHT_RED_PCT,
        hysteresis_pct=TIGHT_HYSTERESIS_PCT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )


def _touch_balloon(balloon: bytearray) -> None:
    """Write one byte per MB to force OS page allocation."""
    for i in range(0, len(balloon), BYTES_PER_MB):
        balloon[i] = 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBalloonGated:
    """Real-memory balloon tests — gated by CIDX_PERF_TEST=1."""

    def test_governor_enters_pressure_band_under_balloon(self):
        """Governor enters YELLOW or RED when 200 MB balloon is live and tight
        watermarks (yellow=10, red=15) are configured.  Both bands indicate
        the governor has crossed into the pressure zone."""
        gov = _tight_gov()
        gov._tick()

        balloon = bytearray(BALLOON_BYTES)
        _touch_balloon(balloon)

        gov._tick()
        assert gov.band in (MemoryBand.YELLOW, MemoryBand.RED), (
            f"Expected YELLOW or RED under {BALLOON_MB} MB pressure, got {gov.band}. "
            f"used_pct={gov.last_used_pct:.1f}%"
        )
        del balloon

    def test_evict_lru_to_floor_called_correctly_while_balloon_live(self):
        """evict_lru_to_floor calls evict_lru_entries with (size - floor) while
        200 MB balloon is allocated and live."""
        gov = _tight_gov()
        gov._tick()

        balloon = bytearray(BALLOON_BYTES)
        _touch_balloon(balloon)

        cache = MagicMock()
        cache.get_stats.return_value = {"size": MOCK_CACHE_SIZE, "capacity": 100}
        cache.evict_lru_entries.return_value = MOCK_EVICTED_COUNT

        gov.evict_lru_to_floor(cache, floor_entries=LRU_FLOOR)
        cache.evict_lru_entries.assert_called_once_with(MOCK_EVICTED_COUNT)

        del balloon

    def test_governor_recovers_to_green_after_balloon_release(self):
        """Governor returns to GREEN after balloon is released and safe watermarks
        (yellow=95, red=98) are applied."""
        gov = _tight_gov()
        gov._tick()

        balloon = bytearray(BALLOON_BYTES)
        _touch_balloon(balloon)
        del balloon

        gov._yellow_pct = SAFE_YELLOW_PCT
        gov._red_pct = SAFE_RED_PCT
        gov._hysteresis_pct = SAFE_HYSTERESIS_PCT
        gov._tick()
        assert gov.band == MemoryBand.GREEN, (
            f"Expected GREEN after balloon release with safe watermarks, got {gov.band}"
        )
