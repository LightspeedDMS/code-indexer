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
    TWELVE_PT_FIVE_PCT,
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


def _readers_healthy() -> FakeMemoryReaders:
    """64 GB-equivalent host, 12.5% used, zero swap -- deep in GREEN.

    Mirrors the reviewer's deterministic reproduction of the Story #1600
    CRITICAL production regression (band stayed RED / admission stayed
    False for a full 30s on a perfectly healthy server).
    """
    used = int(HOST_100GB * TWELVE_PT_FIVE_PCT / PCT_DIVISOR)
    return FakeMemoryReaders(host_total=HOST_100GB, host_used=used)


class TestSyntheticStartupRedFastTrack:
    """Story #1600 CRITICAL regression fix.

    The synthetic pre-sample startup RED (MemoryGovernor.__init__ sets
    band=RED per the fail-safe contract, before any real sample exists)
    must exit immediately once the FIRST real sample proves the server
    healthy -- it must NOT sit out the full red_min_dwell_seconds window.
    Only a RED entered later from GENUINE observed memory pressure still
    gets its full dwell before it may exit (see TestRedMinDwell above,
    which must keep passing unmodified).
    """

    def test_healthy_first_sample_exits_red_immediately_without_dwell(
        self, MemoryGovernor, MemoryBand
    ):
        """Healthy readers (low used_pct, no swap) + first tick completes ->
        admission_allowed() returns True immediately, without waiting out
        the full min_dwell window."""
        readers = _readers_healthy()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
        )

        # Pre-first-tick: fail-safe RED, admission denied (Scenario 4 --
        # unchanged by this fix).
        assert gov.band == MemoryBand.RED
        assert gov.admission_allowed(99.0) is False

        gov._tick()

        # First real sample proves the server healthy: the band must
        # cascade all the way to GREEN in this single tick (mirrors the
        # existing dwell=0 cascade), and admission must be allowed
        # immediately -- NOT after waiting out red_min_dwell_seconds.
        assert gov.band == MemoryBand.GREEN
        assert gov.admission_allowed(99.0) is True

    def test_healthy_first_sample_exits_with_zero_elapsed_wall_clock(
        self, MemoryGovernor, MemoryBand
    ):
        """Even when the injected clock shows ZERO elapsed time since
        construction (the real-world case: sampler ticks almost
        immediately after process start), the synthetic startup RED still
        exits on a healthy first sample -- proving the fix does not depend
        on wall-clock elapsed time, only on whether a genuine RED entry was
        ever recorded."""
        fake_time = [FAKE_TIME_START]

        def _fake_now() -> float:
            return fake_time[0]

        readers = _readers_healthy()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
            time_fn=_fake_now,
        )

        gov._tick()  # fake_time unchanged since construction: elapsed == 0

        assert gov.band == MemoryBand.GREEN
        assert gov.admission_allowed(99.0) is True

    def test_genuine_red_after_startup_exit_still_enforces_full_dwell(
        self, MemoryGovernor, MemoryBand
    ):
        """After the synthetic startup RED fast-tracks out on a healthy
        first sample, a LATER genuine RED entry (real observed pressure)
        must still enforce the full min-dwell window before it can exit --
        proving the fast-track applies ONLY to the synthetic startup RED,
        never to a real pressure-driven RED entered afterward."""
        fake_time = [FAKE_TIME_START]

        def _fake_now() -> float:
            return fake_time[0]

        readers = _readers_healthy()
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
            time_fn=_fake_now,
        )
        gov._tick()
        assert gov.band == MemoryBand.GREEN  # startup RED fast-tracked out

        # Genuine memory pressure appears.
        readers.host_used = int(HOST_100GB * EIGHTY_SEVEN_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.RED  # genuine RED entry, dwell clock starts now

        # Pressure clears, but dwell has NOT elapsed yet -- must stay RED.
        readers.host_used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)
        fake_time[0] = FAKE_TIME_START + DWELL_ELAPSED_BEFORE
        gov._tick()
        assert gov.band == MemoryBand.RED

        # Dwell expires -- now it may exit.
        fake_time[0] = FAKE_TIME_START + DWELL_ELAPSED_AFTER
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
