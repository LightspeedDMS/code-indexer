"""Story #1787 AC17: X-Ray graph-build observability through the EXISTING
governor stats path (record_graph_build_outcome() + get_snapshot()).

Uses the real MemoryGovernor class with the shared FakeMemoryReaders/
make_gov fixtures (no mocking of the class under test), matching this
project's established MemoryGovernor test convention.
"""

from __future__ import annotations

from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)


def _gov():
    from code_indexer.server.services.memory_governor import MemoryGovernor

    readers = FakeMemoryReaders(
        cgroup_v2_max=str(CGROUP_LIMIT_4GB), cgroup_v2_current=str(0)
    )
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()  # establish a real (non-fail-safe-RED) band before asserting
    return gov


def test_gate1_denial_increments_gate1_and_requests_total():
    gov = _gov()

    gov.record_graph_build_outcome(denied_gate="gate1")

    snapshot = gov.get_snapshot()
    assert snapshot["graph_gate1_denials"] == 1
    assert snapshot["graph_gate2_denials"] == 0
    assert snapshot["graph_build_requests_total"] == 1


def test_gate2_denial_increments_gate2_only():
    gov = _gov()

    gov.record_graph_build_outcome(denied_gate="gate2")

    snapshot = gov.get_snapshot()
    assert snapshot["graph_gate2_denials"] == 1
    assert snapshot["graph_gate1_denials"] == 0


def test_red_abort_increments_red_aborts_counter():
    gov = _gov()

    gov.record_graph_build_outcome(red_abort=True)

    assert gov.get_snapshot()["graph_red_aborts"] == 1


def test_memory_limit_abort_increments_memory_limit_aborts_counter():
    gov = _gov()

    gov.record_graph_build_outcome(memory_limit_abort=True)

    assert gov.get_snapshot()["graph_memory_limit_aborts"] == 1


def test_peak_bytes_are_surfaced_as_latest_build_gauges():
    gov = _gov()

    gov.record_graph_build_outcome(
        estimated_peak_bytes=1_000_000, actual_peak_bytes=2_500_000
    )

    snapshot = gov.get_snapshot()
    assert snapshot["last_graph_estimated_peak_bytes"] == 1_000_000
    assert snapshot["last_graph_actual_peak_bytes"] == 2_500_000


def test_an_unrecognized_denied_gate_value_increments_neither_gate_counter_but_still_counts_the_request():
    gov = _gov()

    gov.record_graph_build_outcome(denied_gate="not_a_real_gate")

    snapshot = gov.get_snapshot()
    assert snapshot["graph_gate1_denials"] == 0
    assert snapshot["graph_gate2_denials"] == 0
    assert snapshot["graph_build_requests_total"] == 1


def test_gauges_default_to_none_before_any_build_is_recorded():
    gov = _gov()

    snapshot = gov.get_snapshot()
    assert snapshot["last_graph_estimated_peak_bytes"] is None
    assert snapshot["last_graph_actual_peak_bytes"] is None
    assert snapshot["graph_build_requests_total"] == 0
