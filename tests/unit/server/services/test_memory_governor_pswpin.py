"""pswpin delta (swap-IN rate) tests for MemoryGovernor §3.1.

pswpin_rate is a DELTA between samples, not an absolute value.
delta > 0 AND swap_forces_red=True => forces RED regardless of used_pct.
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    DEFAULT_HOST_TOTAL,
    DEFAULT_HOST_USED,
    PSWPIN_AFTER_SWAP_ACTIVITY,
    PSWPIN_BASELINE,
    PSWPIN_FIRST_SAMPLE_HIGH,
    PSWPIN_STABLE_HIGH,
    RED_PCT,
    YELLOW_PCT,
    FakeMemoryReaders,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def MemoryBand():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryBand as _MB

    return _MB


def _swap_gov(readers, MemoryGovernor, *, swap_forces_red: bool):
    """Build a governor at 25% memory (well below thresholds) with swap config.

    red_min_dwell_seconds=0 so the pre-init RED can cascade out on first tick.
    """
    return MemoryGovernor(
        readers=readers,
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT,
        red_pct=RED_PCT,
        red_min_dwell_seconds=0,
        swap_forces_red=swap_forces_red,
    )


class TestPswpinRateDelta:
    """§3.1 — swap-IN rate is a DELTA per sample, not an absolute counter."""

    def test_delta_positive_forces_red(self, MemoryGovernor, MemoryBand):
        """pswpin increases between samples => band forced to RED."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
            pswpin=PSWPIN_BASELINE,
        )
        gov = _swap_gov(readers, MemoryGovernor, swap_forces_red=True)
        gov._tick()  # baseline established
        readers.pswpin = PSWPIN_AFTER_SWAP_ACTIVITY  # delta > 0
        gov._tick()

        assert gov.band == MemoryBand.RED

    def test_stable_high_absolute_does_not_force_red(self, MemoryGovernor, MemoryBand):
        """High absolute pswpin with zero delta must NOT force RED."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
            pswpin=PSWPIN_STABLE_HIGH,
        )
        gov = _swap_gov(readers, MemoryGovernor, swap_forces_red=True)
        gov._tick()  # baseline = PSWPIN_STABLE_HIGH
        gov._tick()  # delta = 0, still at PSWPIN_STABLE_HIGH

        assert gov.band == MemoryBand.GREEN

    def test_first_sample_delta_treated_as_zero(self, MemoryGovernor, MemoryBand):
        """First sample has no prior baseline; delta is treated as 0 (no RED forced)."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
            pswpin=PSWPIN_FIRST_SAMPLE_HIGH,
        )
        gov = _swap_gov(readers, MemoryGovernor, swap_forces_red=True)
        gov._tick()  # only one sample — no delta computable

        assert gov.band == MemoryBand.GREEN

    def test_swap_forces_red_disabled_ignores_delta(self, MemoryGovernor, MemoryBand):
        """When swap_forces_red=False, pswpin delta has no effect on band."""
        readers = FakeMemoryReaders(
            host_total=DEFAULT_HOST_TOTAL,
            host_used=DEFAULT_HOST_USED,
            pswpin=PSWPIN_BASELINE,
        )
        gov = _swap_gov(readers, MemoryGovernor, swap_forces_red=False)
        gov._tick()
        readers.pswpin = PSWPIN_AFTER_SWAP_ACTIVITY
        gov._tick()

        assert gov.band == MemoryBand.GREEN
