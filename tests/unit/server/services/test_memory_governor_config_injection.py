"""Story 4 — Part 5: Config-injection band transitions (deterministic, CI-safe).

Tests that injecting tight/loose watermarks via config_service forces the
expected band without real memory pressure.

Tests:
- yellow=10/red=15 forces RED at 30% real usage.
- yellow=95/red=98 forces GREEN at 30% real usage.
- Hot-reloading watermarks changes band without restart.
- Hysteresis: oscillating across the gap increments counters exactly once per crossing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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

# Real memory usage (stays constant across all tests)
REAL_USAGE_PCT = 30.0

# Watermarks that force RED at REAL_USAGE_PCT=30%
TIGHT_YELLOW_PCT = 10.0
TIGHT_RED_PCT = 15.0
TIGHT_HYSTERESIS_PCT = 2.0

# Watermarks that force GREEN at REAL_USAGE_PCT=30%
HIGH_YELLOW_PCT = 95.0
HIGH_RED_PCT = 98.0
HIGH_HYSTERESIS_PCT = 1.0

# Default watermarks (governor starts with these; overridden by config_service)
DEFAULT_YELLOW_PCT = 70.0
DEFAULT_RED_PCT = 85.0
DEFAULT_HYSTERESIS_PCT = 10.0
NO_RED_DWELL_SECONDS = 0.0

# Usage values for crossing the default YELLOW boundary (exit = 70-10 = 60)
YELLOW_TERRITORY_PCT = 75.0  # >= yellow=70, < red=85 => YELLOW
BELOW_YELLOW_EXIT_PCT = 55.0  # < yellow_exit=60 => GREEN

# Transition counter expectations
EXACTLY_ONE_CROSSING = 1
NO_SWAP_PAGES_IN = 0


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


def _make_config_svc(
    yellow: float,
    red: float,
    hysteresis: float,
) -> Any:
    """Build a fake config_service whose get_config() returns the given watermarks."""
    cache_cfg = MagicMock()
    cache_cfg.memory_governor_enabled = True
    cache_cfg.memory_governor_yellow_pct = yellow
    cache_cfg.memory_governor_red_pct = red
    cache_cfg.memory_governor_hysteresis_pct = hysteresis
    cache_cfg.memory_governor_swap_forces_red = True
    cache_cfg.memory_governor_red_min_dwell_seconds = NO_RED_DWELL_SECONDS
    cfg = MagicMock()
    cfg.cache_config = cache_cfg
    svc = MagicMock()
    svc.get_config.return_value = cfg
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigInjectionBandTransitions:
    """Config-injection band transitions — no real memory pressure, CI-safe."""

    def test_tight_watermarks_force_red(self):
        """yellow=10/red=15 forces RED even at low real usage (30%)."""
        gov = MemoryGovernor(
            readers=_make_readers(REAL_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=DEFAULT_YELLOW_PCT,
            red_pct=DEFAULT_RED_PCT,
            hysteresis_pct=DEFAULT_HYSTERESIS_PCT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
            config_service=_make_config_svc(
                yellow=TIGHT_YELLOW_PCT,
                red=TIGHT_RED_PCT,
                hysteresis=TIGHT_HYSTERESIS_PCT,
            ),
        )
        gov._tick()
        assert gov.band == MemoryBand.RED, (
            f"Expected RED with yellow={TIGHT_YELLOW_PCT}/red={TIGHT_RED_PCT} "
            f"at {REAL_USAGE_PCT}% usage, got {gov.band}"
        )

    def test_high_watermarks_force_green(self):
        """yellow=95/red=98 forces GREEN at 30% usage."""
        gov = MemoryGovernor(
            readers=_make_readers(REAL_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=DEFAULT_YELLOW_PCT,
            red_pct=DEFAULT_RED_PCT,
            hysteresis_pct=DEFAULT_HYSTERESIS_PCT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
            config_service=_make_config_svc(
                yellow=HIGH_YELLOW_PCT,
                red=HIGH_RED_PCT,
                hysteresis=HIGH_HYSTERESIS_PCT,
            ),
        )
        gov._tick()
        assert gov.band == MemoryBand.GREEN, (
            f"Expected GREEN with yellow={HIGH_YELLOW_PCT}/red={HIGH_RED_PCT} "
            f"at {REAL_USAGE_PCT}% usage, got {gov.band}"
        )

    def test_live_reload_changes_band_without_restart(self):
        """Hot-reloading watermarks via config_service changes band on next tick."""
        gov = MemoryGovernor(
            readers=_make_readers(REAL_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=DEFAULT_YELLOW_PCT,
            red_pct=DEFAULT_RED_PCT,
            hysteresis_pct=DEFAULT_HYSTERESIS_PCT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
            config_service=_make_config_svc(
                yellow=HIGH_YELLOW_PCT,
                red=HIGH_RED_PCT,
                hysteresis=HIGH_HYSTERESIS_PCT,
            ),
        )
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        gov._config_service = _make_config_svc(
            yellow=TIGHT_YELLOW_PCT,
            red=TIGHT_RED_PCT,
            hysteresis=TIGHT_HYSTERESIS_PCT,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

    def test_hysteresis_increments_once_per_crossing(self):
        """Oscillating GREEN<->YELLOW increments each counter exactly once per real crossing."""
        gov = MemoryGovernor(
            readers=_make_readers(REAL_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=DEFAULT_YELLOW_PCT,
            red_pct=DEFAULT_RED_PCT,
            hysteresis_pct=DEFAULT_HYSTERESIS_PCT,
            red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
        )
        gov._tick()  # first tick: fail-safe RED -> GREEN (first_tick, no counter)
        assert gov.band == MemoryBand.GREEN

        # First GREEN->YELLOW crossing
        gov._readers = _make_readers(YELLOW_TERRITORY_PCT)  # type: ignore[attr-defined]
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
        g2y_after_first = gov.counters.green_to_yellow

        # Drop below YELLOW exit threshold -> GREEN
        gov._readers = _make_readers(BELOW_YELLOW_EXIT_PCT)  # type: ignore[attr-defined]
        gov._tick()
        assert gov.band == MemoryBand.GREEN
        y2g_after_first = gov.counters.yellow_to_green

        # Second GREEN->YELLOW crossing
        gov._readers = _make_readers(YELLOW_TERRITORY_PCT)  # type: ignore[attr-defined]
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

        # Second YELLOW->GREEN crossing
        gov._readers = _make_readers(BELOW_YELLOW_EXIT_PCT)  # type: ignore[attr-defined]
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        assert gov.counters.green_to_yellow == g2y_after_first + EXACTLY_ONE_CROSSING
        assert gov.counters.yellow_to_green == y2g_after_first + EXACTLY_ONE_CROSSING
