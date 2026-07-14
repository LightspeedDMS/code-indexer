"""Bug #1374 — swap-in RED override must be corroborated by used_pct.

Production symptom: the governor's swap-in override forced (and HELD) the RED
band whenever pswpin_rate >= swap_pswpin_red_threshold, with ZERO corroboration
from actual memory usage (used_pct).  On production this pinned the band RED
for days with used_pct as low as 10-29% (host had 22GB free of 30GB) — pure
residual swap-in noise from a finished big job.  RED band makes
should_evict_after_shard() return True, so the temporal query dispatch evicts
every quarter-shard HNSW index immediately after use, forcing a 5-15s+ cold
disk reload per quarter on every subsequent query.

Fix: swap can only force/hold RED when used_pct >= yellow_pct (the host is
already at/above the YELLOW watermark — i.e. genuine memory pressure combined
with swap-in).  A host at 25-30% used_pct can no longer be forced into RED or
held there by swap noise alone.  This preserves the legitimate death-spiral
guard from Bug #1225: a host already under real memory pressure AND showing
swap-in activity still gets forced to RED.

Test coverage for:
1. Production scenario (pswpin_rate=2658, used_pct=29.4) does NOT force RED
   from GREEN.
2. Same swap rate at used_pct >= yellow_pct (75%) DOES force/hold RED
   (Bug #1225 guard preserved).
3. Boundary: used_pct exactly == yellow_pct with high swap forces RED
   (>= semantics).
4. Boundary: used_pct one unit below yellow_pct with high swap does NOT
   force RED.
5. GREEN->YELLOW->RED cascade no longer completes to RED in a single tick
   when used_pct is low, even with high swap rate.
6. RED exit/hold: a governor already latched RED does not stay latched
   forever when used_pct drops and dwell has elapsed, despite continued
   high swap-in — swap noise must not indefinitely re-latch RED.
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    FAKE_TIME_START,
    HOST_100GB,
    PCT_DIVISOR,
    RED_MIN_DWELL_SECONDS,
    YELLOW_PCT,
    FakeMemoryReaders,
    make_gov,
)

# ---------------------------------------------------------------------------
# Bug #1374 production values
# ---------------------------------------------------------------------------

PRODUCTION_PSWPIN_DELTA = 2658  # observed production swap-in spiral rate
PRODUCTION_USED_PCT = 29.4  # observed production used_pct (host had 22GB free of 30GB)
MID_RANGE_USED_PCT = 50.0  # below yellow_pct(70), above trivial baseline
CORROBORATED_USED_PCT = 75.0  # >= yellow_pct(70) — genuine memory pressure
PSWPIN_BASE = 10_000  # arbitrary stable baseline for delta computation


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def MemoryBand():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryBand as _MB

    return _MB


def _readers_at_pct(used_pct: float, *, pswpin: int = PSWPIN_BASE) -> FakeMemoryReaders:
    used = int(HOST_100GB * used_pct / PCT_DIVISOR)
    return FakeMemoryReaders(host_total=HOST_100GB, host_used=used, pswpin=pswpin)


class TestSwapUsedPctCorroboration1374:
    """Bug #1374 — swap RED-forcing requires used_pct >= yellow_pct corroboration."""

    def test_production_scenario_swap_high_low_used_pct_does_not_force_red_from_green(
        self, MemoryGovernor, MemoryBand
    ):
        """pswpin_rate=2658 at used_pct=29.4 must NOT force RED from GREEN.

        This is the exact production scenario from Bug #1374: a finished big
        job leaves residual swap-in noise while the host sits at 29.4% used —
        21.7GB free of 30GB.  Without used_pct corroboration this pinned the
        band RED for days, forcing evict-after-use on every quarter-shard.
        """
        readers = _readers_at_pct(PRODUCTION_USED_PCT, pswpin=PSWPIN_BASE)
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()  # baseline pswpin sample; first tick converges to GREEN
        assert gov.band == MemoryBand.GREEN

        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA  # delta = 2658
        gov._tick()

        assert gov.band == MemoryBand.GREEN

    def test_high_used_pct_with_swap_still_forces_red_bug_1225_guard_preserved(
        self, MemoryGovernor, MemoryBand
    ):
        """Same high swap rate at used_pct >= yellow_pct(70) DOES force RED.

        This is Bug #1225's legitimate death-spiral guard: a host already
        under real memory pressure (>= yellow watermark) AND showing swap-in
        activity must still be forced to RED.  The corroboration fix must
        NOT regress this.
        """
        readers = _readers_at_pct(MID_RANGE_USED_PCT, pswpin=PSWPIN_BASE)  # 50%
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()  # first-tick convergence from fail-safe RED -> GREEN
        assert gov.band == MemoryBand.GREEN

        # Raise used_pct into the yellow band (no swap delta yet).
        readers.host_used = int(HOST_100GB * CORROBORATED_USED_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

        # Now inject the high swap-in delta while used_pct stays >= yellow_pct.
        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA  # delta = 2658
        gov._tick()

        assert gov.band == MemoryBand.RED

    def test_boundary_used_pct_equal_yellow_pct_with_swap_forces_red(
        self, MemoryGovernor, MemoryBand
    ):
        """used_pct exactly == yellow_pct with high swap forces RED (>= semantics)."""
        readers = _readers_at_pct(YELLOW_PCT, pswpin=PSWPIN_BASE)  # exactly 70.0
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()

        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA
        gov._tick()

        assert gov.band == MemoryBand.RED

    def test_boundary_used_pct_one_below_yellow_pct_with_swap_does_not_force_red(
        self, MemoryGovernor, MemoryBand
    ):
        """used_pct one unit below yellow_pct with high swap does NOT force RED."""
        readers = _readers_at_pct(YELLOW_PCT - 1.0, pswpin=PSWPIN_BASE)  # 69.0
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()  # first-tick convergence from fail-safe RED -> YELLOW
        # 69.0 sits between yellow_exit(60) and yellow_pct(70), so YELLOW is
        # the correct converged band (not GREEN, not RED).
        assert gov.band == MemoryBand.YELLOW

        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA
        gov._tick()

        assert gov.band != MemoryBand.RED
        assert gov.band == MemoryBand.YELLOW

    def test_cascade_does_not_complete_to_red_when_used_pct_low_despite_high_swap(
        self, MemoryGovernor, MemoryBand
    ):
        """GREEN->YELLOW->RED cascade must not complete to RED when used_pct is
        below yellow_pct, even with a high swap rate — the governor must stay
        fully GREEN (not even partially cascade to YELLOW) because neither the
        used_pct>=yellow_pct branch nor the swap-corroborated branch fires.
        """
        readers = _readers_at_pct(MID_RANGE_USED_PCT, pswpin=PSWPIN_BASE)  # 50%
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA  # delta = 2658
        gov._tick()

        assert gov.band == MemoryBand.GREEN

    def test_red_exits_when_used_pct_drops_despite_continued_high_swap_in(
        self, MemoryGovernor, MemoryBand
    ):
        """A governor genuinely latched RED (used_pct crossed red_pct) must
        exit RED once used_pct drops back down and dwell has elapsed, even
        though swap-in activity continues at a high rate.  Before the fix,
        swap_forces_red was unconditional so `not swap_forces_red` was always
        False under continued swap noise, latching RED indefinitely — this is
        the exact production symptom (pinned RED for days).
        """
        # Tick 1: genuine memory spike above red_pct(85) — band goes RED for
        # real reasons (not swap-forced).
        readers = _readers_at_pct(90.0, pswpin=PSWPIN_BASE)
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=0,
            swap_forces_red=True,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

        # Tick 2: memory pressure resolves (used_pct drops to production
        # value 29.4%) but swap-in noise continues at a high rate.  With
        # dwell=0 the governor must exit RED on this very tick.
        readers.host_used = int(HOST_100GB * PRODUCTION_USED_PCT / PCT_DIVISOR)
        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA  # delta = 2658
        gov._tick()

        assert gov.band != MemoryBand.RED

    def test_red_exits_after_dwell_elapses_with_injected_time_despite_swap_in(
        self, MemoryGovernor, MemoryBand
    ):
        """Same as above but using an injected time_fn and a real nonzero
        dwell: RED must be held while dwell has not elapsed, then exit once
        `red_min_dwell_seconds` has elapsed — swap noise must not indefinitely
        re-latch RED once memory pressure is gone.
        """
        fake_time = [FAKE_TIME_START]

        def _fake_now() -> float:
            return fake_time[0]

        readers = _readers_at_pct(90.0, pswpin=PSWPIN_BASE)
        gov = make_gov(
            readers,
            MemoryGovernor,
            red_min_dwell_seconds=RED_MIN_DWELL_SECONDS,
            swap_forces_red=True,
            time_fn=_fake_now,
        )
        gov._tick()
        assert gov.band == MemoryBand.RED

        # Memory pressure resolves, swap-in noise continues at a high rate.
        readers.host_used = int(HOST_100GB * PRODUCTION_USED_PCT / PCT_DIVISOR)
        readers.pswpin = PSWPIN_BASE + PRODUCTION_PSWPIN_DELTA  # delta = 2658

        # Before dwell expires: still RED.
        fake_time[0] = FAKE_TIME_START + 15.0
        gov._tick()
        assert gov.band == MemoryBand.RED

        # After dwell expires: exits RED despite continued high swap-in.
        fake_time[0] = FAKE_TIME_START + RED_MIN_DWELL_SECONDS + 1.0
        gov._tick()
        assert gov.band != MemoryBand.RED
