"""Story 4 — Part 1: get_snapshot() must return all §3.5 fields.

Tests that MemoryGovernor.get_snapshot() returns the full set of fields
required by the design document §3.5 (band, signal, counters, config echoes, pid).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor

# Hot-reload watermark values (different from CUSTOM_* constructor args below)
LIVE_YELLOW_PCT = 55.0
LIVE_RED_PCT = 75.0
LIVE_HYSTERESIS_PCT = 5.0
LIVE_RED_MIN_DWELL = 10.0
LIVE_SAMPLE_INTERVAL = 1.0
LIVE_SWAP_FORCES_RED = False
LIVE_RSS_INFLATION = 3.0
LIVE_ENABLED = True

# ---------------------------------------------------------------------------
# Named constants (no magic numbers)
# ---------------------------------------------------------------------------

HOST_100GB = 100 * 1024 * 1024 * 1024
GREEN_USAGE_PCT = 30.0  # well below any reasonable yellow threshold
YELLOW_PCT_DEFAULT = 70.0
RED_PCT_DEFAULT = 85.0
HYSTERESIS_PCT_DEFAULT = 10.0
SAMPLE_INTERVAL_DEFAULT = 2.0
RSS_INFLATION_FACTOR_DEFAULT = 2.0

# Custom watermark values for echo tests
CUSTOM_YELLOW_PCT = 65.0
CUSTOM_RED_PCT = 80.0
CUSTOM_HYSTERESIS_PCT = 8.0
CUSTOM_RED_MIN_DWELL = 15.0
CUSTOM_SAMPLE_INTERVAL = 3.0
CUSTOM_RSS_INFLATION = 1.5

# Required §3.5 snapshot fields
REQUIRED_SIGNAL_FIELDS = (
    "band",
    "used_pct",
    "effective_limit_mb",
    "effective_used_mb",
    "basis",
    "pswpin_rate",
    "swap_used_mb",
)
REQUIRED_TRANSITION_COUNTERS = (
    "green_to_yellow",
    "yellow_to_red",
    "red_to_yellow",
    "yellow_to_green",
)
REQUIRED_ACTION_COUNTERS = (
    "shards_evicted_after_use",
    "lru_evictions",
    "trim_calls",
)
REQUIRED_CONFIG_ECHOES = (
    "enabled",
    "yellow_pct",
    "red_pct",
    "hysteresis_pct",
    "red_min_dwell_seconds",
    "sample_interval_seconds",
    "swap_forces_red",
    "rss_inflation_factor",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float, pswpin: int = 0) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_100GB
    vm.used = int(HOST_100GB * used_pct / 100)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = pswpin
    return readers


def _green_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(GREEN_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT_DEFAULT,
        red_pct=RED_PCT_DEFAULT,
        hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
        red_min_dwell_seconds=0.0,
    )
    gov._tick()
    assert gov.band == MemoryBand.GREEN
    return gov


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetSnapshotFields:
    """get_snapshot() must return every field in §3.5."""

    def test_all_signal_fields_present(self):
        """Signal fields (band, used_pct, effective_limit_mb, ...) must be in snapshot."""
        snap = _green_gov().get_snapshot()
        for field in REQUIRED_SIGNAL_FIELDS:
            assert field in snap, f"Missing signal field: {field}"

    def test_all_transition_counters_present(self):
        """Transition counters must appear as top-level snapshot keys (not nested)."""
        snap = _green_gov().get_snapshot()
        for field in REQUIRED_TRANSITION_COUNTERS:
            assert field in snap, f"Missing transition counter: {field}"

    def test_all_action_counters_present(self):
        """Action counters must appear as top-level snapshot keys."""
        snap = _green_gov().get_snapshot()
        for field in REQUIRED_ACTION_COUNTERS:
            assert field in snap, f"Missing action counter: {field}"

    def test_all_config_echo_fields_present(self):
        """Config echo fields (watermarks) must be in snapshot."""
        snap = _green_gov().get_snapshot()
        for field in REQUIRED_CONFIG_ECHOES:
            assert field in snap, f"Missing config echo: {field}"

    def test_pid_field_present_and_matches_process(self):
        """pid must be present and equal os.getpid()."""
        snap = _green_gov().get_snapshot()
        assert "pid" in snap
        assert snap["pid"] == os.getpid()

    def test_band_is_string_not_enum(self):
        """band must be a string value, not a MemoryBand enum instance."""
        snap = _green_gov().get_snapshot()
        assert snap["band"] == "GREEN"
        assert isinstance(snap["band"], str)

    def test_watermarks_echoed_from_constructor(self):
        """All eight watermark config echo values must match constructor args."""
        gov = MemoryGovernor(
            readers=_make_readers(GREEN_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=CUSTOM_YELLOW_PCT,
            red_pct=CUSTOM_RED_PCT,
            hysteresis_pct=CUSTOM_HYSTERESIS_PCT,
            red_min_dwell_seconds=CUSTOM_RED_MIN_DWELL,
            sample_interval_seconds=CUSTOM_SAMPLE_INTERVAL,
            swap_forces_red=False,
            rss_inflation_factor=CUSTOM_RSS_INFLATION,
        )
        gov._tick()
        snap = gov.get_snapshot()
        assert snap["yellow_pct"] == CUSTOM_YELLOW_PCT
        assert snap["red_pct"] == CUSTOM_RED_PCT
        assert snap["hysteresis_pct"] == CUSTOM_HYSTERESIS_PCT
        assert snap["red_min_dwell_seconds"] == CUSTOM_RED_MIN_DWELL
        assert snap["sample_interval_seconds"] == CUSTOM_SAMPLE_INTERVAL
        assert snap["swap_forces_red"] is False
        assert snap["rss_inflation_factor"] == CUSTOM_RSS_INFLATION

    def test_swap_used_mb_is_non_negative(self):
        """swap_used_mb must always be >= 0."""
        snap = _green_gov().get_snapshot()
        assert snap["swap_used_mb"] >= 0

    def test_snapshot_echoes_live_watermarks_after_hot_reload(self):
        """get_snapshot() must reflect live config watermarks, not constructor-frozen values.

        Simulates a Web UI hot-reload: governor constructed with defaults, then
        config_service mock is mutated to return LIVE_* values.  get_snapshot()
        must read live config at call-time and return the new values.
        """
        # Step 1: Build mock with constructor-default watermarks.
        mock_cache_cfg = MagicMock()
        mock_cache_cfg.memory_governor_enabled = True
        mock_cache_cfg.memory_governor_yellow_pct = YELLOW_PCT_DEFAULT
        mock_cache_cfg.memory_governor_red_pct = RED_PCT_DEFAULT
        mock_cache_cfg.memory_governor_hysteresis_pct = HYSTERESIS_PCT_DEFAULT
        mock_cache_cfg.memory_governor_red_min_dwell_seconds = 30.0
        mock_cache_cfg.memory_governor_sample_interval_seconds = SAMPLE_INTERVAL_DEFAULT
        mock_cache_cfg.memory_governor_swap_forces_red = True
        mock_cache_cfg.memory_governor_rss_inflation_factor = (
            RSS_INFLATION_FACTOR_DEFAULT
        )
        mock_config = MagicMock()
        mock_config.cache_config = mock_cache_cfg
        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value = mock_config

        gov = MemoryGovernor(
            readers=_make_readers(GREEN_USAGE_PCT),
            enabled=True,
            start_sampler=False,
            yellow_pct=YELLOW_PCT_DEFAULT,
            red_pct=RED_PCT_DEFAULT,
            hysteresis_pct=HYSTERESIS_PCT_DEFAULT,
            red_min_dwell_seconds=0.0,
            config_service=mock_config_service,
        )
        gov._tick()

        # Step 2: Simulate Web UI hot-reload — mutate the mock to LIVE_* values.
        mock_cache_cfg.memory_governor_enabled = LIVE_ENABLED
        mock_cache_cfg.memory_governor_yellow_pct = LIVE_YELLOW_PCT
        mock_cache_cfg.memory_governor_red_pct = LIVE_RED_PCT
        mock_cache_cfg.memory_governor_hysteresis_pct = LIVE_HYSTERESIS_PCT
        mock_cache_cfg.memory_governor_red_min_dwell_seconds = LIVE_RED_MIN_DWELL
        mock_cache_cfg.memory_governor_sample_interval_seconds = LIVE_SAMPLE_INTERVAL
        mock_cache_cfg.memory_governor_swap_forces_red = LIVE_SWAP_FORCES_RED
        mock_cache_cfg.memory_governor_rss_inflation_factor = LIVE_RSS_INFLATION

        # Step 3: Snapshot must now reflect the LIVE values.
        snap = gov.get_snapshot()
        assert snap["yellow_pct"] == LIVE_YELLOW_PCT, (
            f"yellow_pct echoes constructor default {snap['yellow_pct']}, "
            f"expected live value {LIVE_YELLOW_PCT}"
        )
        assert snap["red_pct"] == LIVE_RED_PCT
        assert snap["hysteresis_pct"] == LIVE_HYSTERESIS_PCT
        assert snap["red_min_dwell_seconds"] == LIVE_RED_MIN_DWELL
        assert snap["sample_interval_seconds"] == LIVE_SAMPLE_INTERVAL
        assert snap["swap_forces_red"] == LIVE_SWAP_FORCES_RED
        assert snap["rss_inflation_factor"] == LIVE_RSS_INFLATION
        assert snap["enabled"] == LIVE_ENABLED
