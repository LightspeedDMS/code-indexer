"""Bug #1225 — memory_governor_swap_pswpin_red_threshold tests.

The governor previously forced RED on ANY swap-in (pswpin_rate > 0), triggering
GREEN<->RED oscillations on trivial OS page-in noise (1-3 pages/interval) while
well below the YELLOW watermark on the staging cluster.

Fix: a configurable minimum swap-in rate threshold.  Only pswpin_rate >= threshold
forces RED.  The master switch (swap_forces_red) remains unchanged — it must be True
AND rate >= threshold for RED to be forced.

Test coverage for:
1. pswpin_rate BELOW threshold does NOT force RED (band stays GREEN at low mem-usage).
2. pswpin_rate AT threshold forces RED (death-spiral guard preserved).
3. pswpin_rate ABOVE threshold forces RED (sustained high rate still triggers).
4. swap_forces_red=False (master off) — never forces RED regardless of pswpin.
5. Default threshold is 100, exposed in get_snapshot echo.
6. Hot-reload: changing the threshold via live config takes effect on next tick.
7. GOV-005 NOT emitted when pswpin < threshold.
8. GOV-005 emitted when pswpin >= threshold and band is RED.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GB = 1024 * 1024 * 1024

# Host: 32 GB total, 8 GB used = 25% — well below 70/85% watermarks
HOST_TOTAL = 32 * GB
HOST_USED = 8 * GB

YELLOW_PCT = 70.0
RED_PCT = 85.0
HYSTERESIS_PCT = 10.0

# Threshold scenarios
DEFAULT_THRESHOLD = 100  # expected default in CacheConfig
NOISE_BELOW_THRESHOLD = 1  # typical OS idle noise — must NOT force RED
NOISE_BELOW_THRESHOLD_50 = 50  # still below default 100 — must NOT force RED
THRESHOLD_EXACTLY = 100  # AT threshold — must force RED
SPIRAL_RATE = 3630  # staging observed spiral — well above threshold

# pswpin absolute values used across ticks (we need deltas, so we simulate two ticks)
PSWPIN_BASE = 10000  # arbitrary stable baseline
PSWPIN_AFTER_NOISE = PSWPIN_BASE + NOISE_BELOW_THRESHOLD  # delta = 1
PSWPIN_AFTER_NOISE_50 = PSWPIN_BASE + NOISE_BELOW_THRESHOLD_50  # delta = 50
PSWPIN_AFTER_THRESHOLD = PSWPIN_BASE + THRESHOLD_EXACTLY  # delta = 100
PSWPIN_AFTER_SPIRAL = PSWPIN_BASE + SPIRAL_RATE  # delta = 3630

# Bug #1374: used_pct corroboration required for swap to force/hold RED.
DEFAULT_USED_PCT = 25.0  # well below 70/85% watermarks (the 8GB/32GB fixture default)
CORROBORATED_USED_PCT = 72.0  # strictly within [yellow_exit=60, red_exit=75) band


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_readers(
    pswpin_start: int = PSWPIN_BASE, *, used_pct: float = DEFAULT_USED_PCT
) -> MagicMock:
    """FakeMemoryReaders returning `used_pct`% host usage (default 25%).

    Uses math.ceil for the byte conversion so a boundary value (e.g. exactly
    YELLOW_PCT) lands at-or-slightly-above the target percentage rather than
    a hair below it due to int()-truncation, matching `>=` semantics.
    """
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_TOTAL
    vm.used = math.ceil(HOST_TOTAL * used_pct / 100.0)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = pswpin_start
    return readers


def _make_config_svc(
    *,
    swap_forces_red: bool = True,
    threshold: int = DEFAULT_THRESHOLD,
    yellow: float = YELLOW_PCT,
    red: float = RED_PCT,
    hysteresis: float = HYSTERESIS_PCT,
) -> Any:
    """Build a fake config_service with the given governor knobs."""
    cache_cfg = MagicMock()
    cache_cfg.memory_governor_enabled = True
    cache_cfg.memory_governor_yellow_pct = yellow
    cache_cfg.memory_governor_red_pct = red
    cache_cfg.memory_governor_hysteresis_pct = hysteresis
    cache_cfg.memory_governor_swap_forces_red = swap_forces_red
    cache_cfg.memory_governor_red_min_dwell_seconds = 0.0
    cache_cfg.memory_governor_rss_inflation_factor = 2.0
    cache_cfg.memory_governor_sample_interval_seconds = 2.0
    cache_cfg.memory_governor_swap_pswpin_red_threshold = threshold
    cfg = MagicMock()
    cfg.cache_config = cache_cfg
    svc = MagicMock()
    svc.get_config.return_value = cfg
    return svc


def _gov_with_threshold(
    readers: MagicMock,
    *,
    threshold: int = DEFAULT_THRESHOLD,
    swap_forces_red: bool = True,
    config_service: Any = None,
) -> Any:
    """Build a MemoryGovernor with the given threshold, dwell=0 for quick testing."""
    from code_indexer.server.services.memory_governor import MemoryGovernor

    return MemoryGovernor(
        readers=readers,
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT,
        red_pct=RED_PCT,
        hysteresis_pct=HYSTERESIS_PCT,
        red_min_dwell_seconds=0,
        swap_forces_red=swap_forces_red,
        swap_pswpin_red_threshold=threshold,
        config_service=config_service,
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestSwapThresholdBelowDoesNotForceRed:
    """pswpin_rate below threshold must NOT force RED at low memory usage."""

    def test_noise_rate_1_stays_green(self):
        """Single page-in (noise level 1) does NOT force RED."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()  # baseline
        readers.read_pswpin.return_value = PSWPIN_AFTER_NOISE  # delta = 1
        gov._tick()

        assert gov.band == MemoryBand.GREEN

    def test_noise_rate_50_stays_green(self):
        """Rate of 50 pages/interval (below 100 default) does NOT force RED."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_NOISE_50  # delta = 50
        gov._tick()

        assert gov.band == MemoryBand.GREEN


class TestSwapThresholdAtOrAboveForcsRed:
    """pswpin_rate AT or ABOVE threshold must force RED (death-spiral guard)."""

    def test_rate_at_threshold_forces_red(self):
        """Rate exactly AT threshold (100), corroborated by used_pct >= yellow_pct
        (Bug #1374), forces RED."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE, used_pct=CORROBORATED_USED_PCT)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_THRESHOLD  # delta = 100
        gov._tick()

        assert gov.band == MemoryBand.RED

    def test_spiral_rate_forces_red(self):
        """Staging spiral rate (3630), corroborated by used_pct >= yellow_pct
        (Bug #1374), forces RED."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE, used_pct=CORROBORATED_USED_PCT)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_SPIRAL  # delta = 3630
        gov._tick()

        assert gov.band == MemoryBand.RED


class TestSwapThresholdCorroboratedWithUsedPct:
    """Bug #1374 — swap RED-forcing requires used_pct >= yellow_pct.

    The governor previously forced/held RED purely from pswpin_rate >=
    threshold, with ZERO corroboration from actual memory usage.  On
    production this pinned the band RED for days with used_pct as low as
    10-29% (host had 22GB free of 30GB) — pure residual swap-in noise.
    """

    def test_threshold_rate_at_default_low_used_pct_does_not_force_red(self):
        """Rate AT threshold (100) at the default 25% used_pct fixture must
        NOT force RED — used_pct corroboration is required."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE)  # default used_pct=25.0
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_THRESHOLD  # delta = 100
        gov._tick()

        assert gov.band == MemoryBand.GREEN

    def test_threshold_rate_at_exact_yellow_pct_boundary_forces_red(self):
        """Rate AT threshold(100) with used_pct exactly == yellow_pct(70.0)
        DOES force RED — boundary case proving `>=` semantics."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE, used_pct=YELLOW_PCT)  # exactly 70.0
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_THRESHOLD  # delta = 100
        gov._tick()

        assert gov.band == MemoryBand.RED


class TestMasterSwitchOffNeverForcesRed:
    """swap_forces_red=False — master off means threshold is irrelevant."""

    def test_master_off_ignores_high_rate(self):
        """Even spiral rate does NOT force RED when master switch is off."""
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE)
        gov = _gov_with_threshold(
            readers, swap_forces_red=False, threshold=DEFAULT_THRESHOLD
        )
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_SPIRAL  # delta = 3630
        gov._tick()

        assert gov.band == MemoryBand.GREEN


class TestDefaultThresholdExposed:
    """Default threshold is 100 and appears in get_snapshot echo."""

    def test_cache_config_has_correct_default(self):
        """CacheConfig.memory_governor_swap_pswpin_red_threshold defaults to 100."""
        from code_indexer.server.utils.config_manager import CacheConfig

        cfg = CacheConfig()
        assert cfg.memory_governor_swap_pswpin_red_threshold == DEFAULT_THRESHOLD

    def test_get_snapshot_exposes_threshold_no_config_service(self):
        """get_snapshot echoes swap_pswpin_red_threshold (constructor default, no config_service)."""
        readers = _fake_readers()
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        snap = gov.get_snapshot()

        assert "swap_pswpin_red_threshold" in snap
        assert snap["swap_pswpin_red_threshold"] == DEFAULT_THRESHOLD

    def test_get_snapshot_exposes_threshold_from_live_config(self):
        """get_snapshot echoes swap_pswpin_red_threshold from live config_service."""
        LIVE_THRESHOLD = 200
        readers = _fake_readers()
        svc = _make_config_svc(threshold=LIVE_THRESHOLD)
        gov = _gov_with_threshold(readers, config_service=svc)
        snap = gov.get_snapshot()

        assert snap["swap_pswpin_red_threshold"] == LIVE_THRESHOLD


class TestHotReloadThreshold:
    """Changing threshold via live config takes effect on the next tick."""

    def test_raise_threshold_stops_forcing_red(self):
        """Threshold raised above current rate: band exits RED on next tick.

        used_pct is held at CORROBORATED_USED_PCT throughout so swap can
        legitimately force RED (Bug #1374 corroboration requirement); once
        the threshold is raised above the ongoing rate, swap no longer
        forces RED and the band settles back to YELLOW (used_pct alone is
        within the yellow band at this level, not GREEN).
        """
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE, used_pct=CORROBORATED_USED_PCT)
        # Start with threshold=1 so rate=50 forces RED
        svc = _make_config_svc(threshold=1)
        gov = _gov_with_threshold(readers, threshold=1, config_service=svc)
        gov._tick()  # baseline
        readers.read_pswpin.return_value = PSWPIN_AFTER_NOISE_50  # delta = 50
        gov._tick()
        assert gov.band == MemoryBand.RED  # low threshold triggered RED

        # Now raise threshold above the ongoing rate via live config
        svc.get_config.return_value.cache_config.memory_governor_swap_pswpin_red_threshold = 200
        readers.read_pswpin.return_value = PSWPIN_AFTER_NOISE_50  # same delta = 50
        gov._tick()

        assert gov.band == MemoryBand.YELLOW  # threshold no longer triggered

    def test_lower_threshold_starts_forcing_red(self):
        """Threshold lowered below current rate: band becomes RED on next tick.

        used_pct is held at CORROBORATED_USED_PCT throughout (Bug #1374
        corroboration requirement) so the mid-test resting state is YELLOW
        (used_pct alone is within the yellow band), not GREEN.
        """
        from code_indexer.server.services.memory_governor import MemoryBand

        readers = _fake_readers(PSWPIN_BASE, used_pct=CORROBORATED_USED_PCT)
        # Start with threshold=200 so delta=100 does NOT force RED
        svc = _make_config_svc(threshold=200)
        gov = _gov_with_threshold(readers, threshold=200, config_service=svc)
        gov._tick()  # baseline — pswpin = PSWPIN_BASE
        readers.read_pswpin.return_value = PSWPIN_AFTER_THRESHOLD  # delta = 100
        gov._tick()
        assert gov.band == MemoryBand.YELLOW  # high threshold, no RED

        # Now lower threshold below the ongoing rate AND advance pswpin so delta is 100 again
        svc.get_config.return_value.cache_config.memory_governor_swap_pswpin_red_threshold = 50
        readers.read_pswpin.return_value = (
            PSWPIN_AFTER_THRESHOLD + THRESHOLD_EXACTLY
        )  # delta = 100
        gov._tick()

        assert gov.band == MemoryBand.RED  # threshold now catches it


class TestGov005LogGating:
    """GOV-005 should only emit when pswpin_rate >= threshold AND band is RED."""

    def test_gov005_not_emitted_below_threshold(self):
        """No GOV-005 when pswpin_rate < threshold."""
        readers = _fake_readers(PSWPIN_BASE)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_NOISE  # delta = 1

        with patch(
            "code_indexer.server.services.memory_governor.logger"
        ) as mock_logger:
            gov._tick()
            # No warning call containing GOV-005 should have been made
            gov005_calls = [
                c for c in mock_logger.warning.call_args_list if "GOV-005" in str(c)
            ]
            assert len(gov005_calls) == 0

    def test_gov005_emitted_at_threshold(self):
        """GOV-005 is emitted when pswpin_rate >= threshold, corroborated by
        used_pct >= yellow_pct (Bug #1374), forces RED."""
        readers = _fake_readers(PSWPIN_BASE, used_pct=CORROBORATED_USED_PCT)
        gov = _gov_with_threshold(readers, threshold=DEFAULT_THRESHOLD)
        gov._tick()
        readers.read_pswpin.return_value = PSWPIN_AFTER_THRESHOLD  # delta = 100

        with patch(
            "code_indexer.server.services.memory_governor.logger"
        ) as mock_logger:
            gov._tick()
            gov005_calls = [
                c for c in mock_logger.warning.call_args_list if "GOV-005" in str(c)
            ]
            assert len(gov005_calls) == 1


# ---------------------------------------------------------------------------
# Round-trip tests: _update_cache_setting whitelist (Bug #1225 defect fix)
# ---------------------------------------------------------------------------


class _CacheSettingUpdater:
    """Minimal stub that calls _update_cache_setting on a real CacheConfig.

    Mirrors the helper in test_memory_governor_config_story2.py so tests
    exercise the actual config_service whitelist without MagicMock bypass.
    """

    def __init__(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        self._config = ServerConfig(server_dir="/tmp/test")
        assert self._config.cache_config is not None

    def update(self, key: str, value: Any) -> None:
        from code_indexer.server.services.config_service import ConfigService

        svc = object.__new__(ConfigService)
        svc._update_cache_setting(self._config, key, value)

    @property
    def cache(self):
        return self._config.cache_config


class TestUpdateCacheSettingRoundTrip:
    """_update_cache_setting must accept memory_governor_swap_pswpin_red_threshold.

    Before the Bug #1225 fix this key hit the `else: raise ValueError("Unknown
    cache setting: ...")` branch, making the entire cache config section
    unsaveable from the Web UI once the field appeared in the form.
    """

    def test_valid_value_persisted(self):
        """update('memory_governor_swap_pswpin_red_threshold', '150') does not raise
        and the config reflects value 150."""
        f = _CacheSettingUpdater()
        f.update("memory_governor_swap_pswpin_red_threshold", "150")
        assert f.cache.memory_governor_swap_pswpin_red_threshold == 150

    def test_zero_is_valid(self):
        """Threshold of 0 means every swap-in forces RED — valid (disables noise filter)."""
        f = _CacheSettingUpdater()
        f.update("memory_governor_swap_pswpin_red_threshold", "0")
        assert f.cache.memory_governor_swap_pswpin_red_threshold == 0

    def test_negative_value_raises(self):
        """Negative threshold must raise ValueError."""
        f = _CacheSettingUpdater()
        with pytest.raises(ValueError, match="non-negative"):
            f.update("memory_governor_swap_pswpin_red_threshold", "-1")

    def test_non_integer_raises(self):
        """Non-integer string must raise ValueError (int() coercion fails)."""
        f = _CacheSettingUpdater()
        with pytest.raises((ValueError, TypeError)):
            f.update("memory_governor_swap_pswpin_red_threshold", "abc")

    def test_blank_falls_back_to_default(self):
        """Bug #1396: blank string must NOT raise -- it must fall back to the
        documented default (100), matching the size-cap fields' existing
        blank-tolerance idiom rather than crashing on int('')."""
        f = _CacheSettingUpdater()
        f.update("memory_governor_swap_pswpin_red_threshold", "")
        assert f.cache.memory_governor_swap_pswpin_red_threshold == 100
