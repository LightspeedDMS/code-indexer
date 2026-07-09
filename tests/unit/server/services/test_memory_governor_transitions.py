"""Band state machine transition tests for MemoryGovernor §3.2.

9 tests: entry/exit thresholds, counters, hysteresis, no-direct-GREEN<->RED.
All tests drive _tick() synchronously (start_sampler=False, red_min_dwell_seconds=0).
"""

from __future__ import annotations

import pytest

from tests.unit.server.services.test_memory_governor_fixtures import (
    EXPECTED_AT_LEAST_ONE,
    EXPECTED_ONE_TRANSITION,
    EXPECTED_ZERO_TRANSITIONS,
    FIFTY_PCT,
    FIFTY_NINE_PCT,
    HOST_100GB,
    HYSTERESIS_OSCILLATION_ROUNDS,
    HYSTERESIS_PCT,
    PCT_DIVISOR,
    RED_PCT,
    SEVENTY_TWO_PCT,
    SEVENTY_FOUR_PCT,
    SEVENTY_SIX_PCT,
    SIXTY_TWO_PCT,
    SIXTY_EIGHT_PCT,
    EIGHTY_SEVEN_PCT,
    NINETY_PCT,
    YELLOW_PCT,
    FakeMemoryReaders,
    make_gov,
)

# Derive exit thresholds from the named constants to document the arithmetic
_RED_EXIT_PCT = RED_PCT - HYSTERESIS_PCT  # 75.0
_YELLOW_EXIT_PCT = YELLOW_PCT - HYSTERESIS_PCT  # 60.0


@pytest.fixture()
def MemoryGovernor():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryGovernor as _MG

    return _MG


@pytest.fixture()
def MemoryBand():  # noqa: N802
    from code_indexer.server.services.memory_governor import MemoryBand as _MB

    return _MB


def _readers_at(pct: float) -> FakeMemoryReaders:
    used = int(HOST_100GB * pct / PCT_DIVISOR)
    return FakeMemoryReaders(host_total=HOST_100GB, host_used=used)


def _gov(pct: float, MemoryGovernor):
    return make_gov(_readers_at(pct), MemoryGovernor)


class TestBandTransitions:
    """§3.2 — 9 band-transition tests."""

    def test_below_yellow_is_green(self, MemoryGovernor, MemoryBand):
        gov = _gov(FIFTY_PCT, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.GREEN

    def test_at_yellow_enters_yellow(self, MemoryGovernor, MemoryBand):
        gov = _gov(YELLOW_PCT, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

    def test_at_red_enters_red(self, MemoryGovernor, MemoryBand):
        gov = _gov(RED_PCT, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.RED

    def test_green_to_yellow_counter(self, MemoryGovernor, MemoryBand):
        readers = _readers_at(FIFTY_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        readers.host_used = int(HOST_100GB * SEVENTY_TWO_PCT / PCT_DIVISOR)
        gov._tick()

        assert gov.band == MemoryBand.YELLOW
        assert gov.counters.green_to_yellow == EXPECTED_ONE_TRANSITION

    def test_yellow_to_red_counter(self, MemoryGovernor, MemoryBand):
        readers = _readers_at(SEVENTY_TWO_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

        readers.host_used = int(HOST_100GB * EIGHTY_SEVEN_PCT / PCT_DIVISOR)
        gov._tick()

        assert gov.band == MemoryBand.RED
        assert gov.counters.yellow_to_red == EXPECTED_ONE_TRANSITION

    def test_no_direct_green_to_red(self, MemoryGovernor, MemoryBand):
        """Jump from GREEN past RED threshold; must visit YELLOW (counter evidence)."""
        readers = _readers_at(FIFTY_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.GREEN

        readers.host_used = int(HOST_100GB * NINETY_PCT / PCT_DIVISOR)
        gov._tick()

        assert gov.band == MemoryBand.RED
        assert gov.counters.green_to_yellow >= EXPECTED_AT_LEAST_ONE
        assert gov.counters.yellow_to_red >= EXPECTED_AT_LEAST_ONE

    def test_red_to_yellow_requires_hysteresis(self, MemoryGovernor, MemoryBand):
        """RED exits only when used_pct < RED_PCT - HYSTERESIS_PCT."""
        readers = _readers_at(EIGHTY_SEVEN_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.RED

        # Above _RED_EXIT_PCT — stays RED
        readers.host_used = int(HOST_100GB * SEVENTY_SIX_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.RED
        assert SEVENTY_SIX_PCT > _RED_EXIT_PCT  # documents why it stays RED

        # Below _RED_EXIT_PCT — moves to YELLOW
        readers.host_used = int(HOST_100GB * SEVENTY_FOUR_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
        assert SEVENTY_FOUR_PCT < _RED_EXIT_PCT  # documents why it exits
        assert gov.counters.red_to_yellow == EXPECTED_ONE_TRANSITION

    def test_yellow_to_green_requires_hysteresis(self, MemoryGovernor, MemoryBand):
        """YELLOW exits only when used_pct < YELLOW_PCT - HYSTERESIS_PCT."""
        readers = _readers_at(SEVENTY_TWO_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW

        # Above _YELLOW_EXIT_PCT — stays YELLOW
        readers.host_used = int(HOST_100GB * SIXTY_TWO_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
        assert SIXTY_TWO_PCT > _YELLOW_EXIT_PCT  # documents why it stays YELLOW

        # Below _YELLOW_EXIT_PCT — moves to GREEN
        readers.host_used = int(HOST_100GB * FIFTY_NINE_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.GREEN
        assert FIFTY_NINE_PCT < _YELLOW_EXIT_PCT  # documents why it exits
        assert gov.counters.yellow_to_green == EXPECTED_ONE_TRANSITION

    def test_hysteresis_prevents_flapping(self, MemoryGovernor, MemoryBand):
        """Oscillating around YELLOW entry doesn't re-increment green_to_yellow."""
        readers = _readers_at(FIFTY_PCT)
        gov = make_gov(readers, MemoryGovernor)
        gov._tick()

        # Enter YELLOW once
        readers.host_used = int(HOST_100GB * SEVENTY_TWO_PCT / PCT_DIVISOR)
        gov._tick()
        assert gov.band == MemoryBand.YELLOW
        assert gov.counters.green_to_yellow == EXPECTED_ONE_TRANSITION

        # Oscillate: 68% (below entry=70%, above exit=60%) <-> 72% (above entry)
        for _ in range(HYSTERESIS_OSCILLATION_ROUNDS):
            readers.host_used = int(HOST_100GB * SIXTY_EIGHT_PCT / PCT_DIVISOR)
            gov._tick()
            assert gov.band == MemoryBand.YELLOW  # hysteresis holds

            readers.host_used = int(HOST_100GB * SEVENTY_TWO_PCT / PCT_DIVISOR)
            gov._tick()
            assert gov.band == MemoryBand.YELLOW

        assert gov.counters.yellow_to_green == EXPECTED_ZERO_TRANSITIONS
        assert gov.counters.green_to_yellow == EXPECTED_ONE_TRANSITION
