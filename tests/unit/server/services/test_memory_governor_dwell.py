"""RED min-dwell tests for MemoryGovernor §3.2.

RED cannot be exited until red_min_dwell_seconds have elapsed.
Tests use an injectable time_fn to avoid real sleeps.
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    DWELL_ELAPSED_AFTER,
    DWELL_ELAPSED_BEFORE,
    EIGHTY_SEVEN_PCT,
    FAKE_TIME_START,
    HOST_100GB,
    PCT_DIVISOR,
    RED_MIN_DWELL_SECONDS,
    SEVENTY_FOUR_PCT,
    FakeMemoryReaders,
    make_gov,
)


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def MemoryBand():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryBand as _MB

    return _MB


def _readers_red() -> FakeMemoryReaders:
    used = int(HOST_100GB * EIGHTY_SEVEN_PCT / PCT_DIVISOR)
    return FakeMemoryReaders(host_total=HOST_100GB, host_used=used)


def _readers_low() -> FakeMemoryReaders:
    used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)
    return FakeMemoryReaders(host_total=HOST_100GB, host_used=used)


class TestRedMinDwell:
    """§3.2 — RED min-dwell: cannot exit RED until dwell_seconds elapsed."""

    def test_dwell_blocks_exit(self, MemoryGovernor, MemoryBand):
        """Within dwell period, dropping below exit threshold keeps band RED."""
        readers = _readers_red()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

        readers.host_used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.RED  # dwell not expired yet

    def test_dwell_zero_allows_immediate_exit(self, MemoryGovernor, MemoryBand):
        """With dwell=0, RED exits as soon as usage drops below exit threshold."""
        readers = _readers_red()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

        readers.host_used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

    def test_fake_time_dwell_expires(self, MemoryGovernor, MemoryBand):
        """Dwell expires when injected time advances past threshold."""
        fake_time = [FAKE_TIME_START]

        def _fake_now() -> float:
            return fake_time[0]

        readers = _readers_red()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
            time_fn=_fake_now,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

        readers.host_used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)

        # Before dwell expires
        fake_time[0] = FAKE_TIME_START + DWELL_ELAPSED_BEFORE
        gov._tick()
        assert gov.band == MemoryBand.RED

        # After dwell expires
        fake_time[0] = FAKE_TIME_START + DWELL_ELAPSED_AFTER
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
