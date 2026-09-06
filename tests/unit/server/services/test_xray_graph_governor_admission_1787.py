"""Story #1787 AC12/AC13: two-gate admission and phase-boundary checks.

Uses the real MemoryGovernor class with the shared FakeMemoryReaders/
make_gov fixtures (no mocking of the module under test), matching this
project's established MemoryGovernor test convention.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.xray_graph_governor.admission import (
    DEFAULT_SAFETY_FACTOR,
    PreBindStats,
    check_gate1,
    check_gate2,
    check_phase_boundary,
    estimate_gate1_bytes,
    estimate_gate2_bytes,
    watermark_for,
)
from code_indexer.server.services.xray_graph_governor.status import (
    GraphBuildAbortStatus,
    GraphBuildPhase,
)
from tests.unit.server.services.test_memory_governor_fixtures import (
    CGROUP_LIMIT_4GB,
    FakeMemoryReaders,
    make_gov,
)

_ONE_GIB = 1024 * 1024 * 1024


def _gov_at_used_pct(used_pct: float):
    """Build a real MemoryGovernor ticked once so its band/used_pct
    reflect `used_pct` deterministically."""
    if not 0.0 <= used_pct <= 100.0:
        raise ValueError(f"used_pct must be within [0, 100], got {used_pct}")
    from code_indexer.server.services.memory_governor import MemoryGovernor

    limit = CGROUP_LIMIT_4GB
    current = int(limit * used_pct / 100.0)
    readers = FakeMemoryReaders(
        cgroup_v2_max=str(limit), cgroup_v2_current=str(current)
    )
    gov = make_gov(readers, MemoryGovernor)
    gov._tick()
    return gov


def test_small_estimate_relative_to_limit_yields_a_high_watermark():
    watermark = watermark_for(
        estimated_peak_bytes=_ONE_GIB, cgroup_limit_bytes=100 * _ONE_GIB
    )
    assert watermark == pytest.approx(80.0)  # clamped at the max watermark


def test_large_estimate_relative_to_limit_yields_a_low_watermark():
    watermark = watermark_for(
        estimated_peak_bytes=95 * _ONE_GIB, cgroup_limit_bytes=100 * _ONE_GIB
    )
    assert watermark == pytest.approx(10.0)  # clamped at the min watermark


def test_zero_cgroup_limit_returns_the_minimum_watermark_never_divides_by_zero():
    assert watermark_for(estimated_peak_bytes=1, cgroup_limit_bytes=0) == 10.0


def test_watermark_for_negative_estimate_raises_value_error():
    with pytest.raises(ValueError):
        watermark_for(estimated_peak_bytes=-1, cgroup_limit_bytes=100)


def test_java_source_bytes_use_the_seeded_k():
    estimate = estimate_gate1_bytes({"java": 1000}, safety_factor=1.0)
    assert estimate == 19000  # 1000 * 19.0 * 1.0


def test_safety_factor_scales_the_gate1_estimate_linearly():
    assert estimate_gate1_bytes({"java": 1000}, safety_factor=2.0) == 38000


def test_negative_source_bytes_raises_value_error():
    with pytest.raises(ValueError):
        estimate_gate1_bytes({"java": -1})


def test_none_source_bytes_by_language_raises_value_error():
    # Intentionally passing None to verify estimate_gate1_bytes' own
    # runtime validation (its public callers are not statically typed
    # enforced across the process boundary this will eventually cross).
    with pytest.raises(ValueError):
        estimate_gate1_bytes(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_safety_factor", [0, -1.0])
def test_zero_or_negative_safety_factor_raises_value_error(bad_safety_factor):
    with pytest.raises(ValueError):
        estimate_gate1_bytes({"java": 1000}, safety_factor=bad_safety_factor)


def test_check_gate1_admits_and_records_estimate_when_headroom_is_sufficient():
    gov = _gov_at_used_pct(5.0)  # GREEN, low usage

    decision = check_gate1(gov, {"java": 1}, cgroup_limit_bytes=1000 * _ONE_GIB)

    assert decision.allowed is True
    assert decision.abort_status is None
    snapshot = gov.get_snapshot()
    assert snapshot["graph_build_requests_total"] == 1
    assert snapshot["graph_gate1_denials"] == 0
    assert snapshot["last_graph_estimated_peak_bytes"] is not None


def test_check_gate1_denies_with_admission_denied_gate1_when_headroom_is_insufficient():
    gov = _gov_at_used_pct(30.0)  # used_pct comfortably above the min watermark (10%)

    # A huge estimate relative to a tiny cgroup limit forces the
    # watermark down to its minimum (10%) -- below the governor's real
    # 30% used_pct, so admission_allowed() must refuse.
    decision = check_gate1(gov, {"java": 10**9}, cgroup_limit_bytes=1)

    assert decision.allowed is False
    assert decision.abort_status == GraphBuildAbortStatus.ADMISSION_DENIED_GATE1
    assert gov.get_snapshot()["graph_gate1_denials"] == 1


def test_check_gate1_none_governor_raises_value_error():
    with pytest.raises(ValueError):
        check_gate1(None, {"java": 1}, cgroup_limit_bytes=1)


def test_estimate_gate2_bytes_exact_counts_produce_the_documented_weighted_sum():
    stats = PreBindStats(
        declaration_count=10, call_site_count=20, candidate_edge_count=30
    )
    estimate = estimate_gate2_bytes(stats, safety_factor=1.0)
    # 30*8 (candidate) + 20*24 (reference) + 10*64 (symbol) = 240+480+640 = 1360
    assert estimate == 1360


def test_estimate_gate2_bytes_negative_counts_raise_value_error():
    with pytest.raises(ValueError):
        estimate_gate2_bytes(
            PreBindStats(
                declaration_count=-1, call_site_count=0, candidate_edge_count=0
            )
        )


def test_estimate_gate2_bytes_none_pre_bind_stats_raises_value_error():
    # Intentionally passing None to verify estimate_gate2_bytes' own
    # runtime validation (its public callers are not statically typed
    # enforced across the process boundary this will eventually cross).
    with pytest.raises(ValueError):
        estimate_gate2_bytes(None)  # type: ignore[arg-type]


def test_check_gate2_admits_when_headroom_is_sufficient():
    gov = _gov_at_used_pct(5.0)
    stats = PreBindStats(declaration_count=1, call_site_count=1, candidate_edge_count=1)

    decision = check_gate2(gov, stats, cgroup_limit_bytes=1000 * _ONE_GIB)

    assert decision.allowed is True
    assert gov.get_snapshot()["graph_gate2_denials"] == 0


def test_check_gate2_denies_with_admission_denied_gate2_distinct_from_gate1():
    gov = _gov_at_used_pct(30.0)
    stats = PreBindStats(
        declaration_count=10**7, call_site_count=10**7, candidate_edge_count=10**7
    )

    decision = check_gate2(gov, stats, cgroup_limit_bytes=1)

    assert decision.allowed is False
    assert decision.abort_status == GraphBuildAbortStatus.ADMISSION_DENIED_GATE2
    snapshot = gov.get_snapshot()
    assert snapshot["graph_gate2_denials"] == 1
    assert snapshot["graph_gate1_denials"] == 0, (
        "gate2 denial must not be miscounted as a gate1 denial"
    )


def test_check_gate2_none_governor_raises_value_error():
    stats = PreBindStats(declaration_count=1, call_site_count=1, candidate_edge_count=1)
    with pytest.raises(ValueError):
        check_gate2(None, stats, cgroup_limit_bytes=1)


def test_check_phase_boundary_red_band_aborts_with_aborted_memory_pressure_and_increments_counter():
    gov = _gov_at_used_pct(95.0)  # above red_pct (85.0 in make_gov's defaults)
    assert gov.band.value == "RED"

    decision = check_phase_boundary(gov, GraphBuildPhase.BIND)

    assert decision.allowed is False
    assert decision.abort_status == GraphBuildAbortStatus.ABORTED_MEMORY_PRESSURE
    assert gov.get_snapshot()["graph_red_aborts"] == 1


def test_check_phase_boundary_non_red_band_passes_without_aborting():
    gov = _gov_at_used_pct(5.0)

    decision = check_phase_boundary(gov, GraphBuildPhase.EXTRACT)

    assert decision.allowed is True
    assert decision.abort_status is None


def test_check_phase_boundary_repeated_successful_checks_never_inflate_graph_build_requests_total():
    """A single build checks this boundary up to 4 times (once per
    phase). Counting every PASS would corrupt
    graph_build_requests_total's 'how many builds were requested'
    semantics -- this test proves the counter stays at zero across 4
    consecutive successful boundary checks."""
    gov = _gov_at_used_pct(5.0)

    for phase in GraphBuildPhase:
        decision = check_phase_boundary(gov, phase)
        assert decision.allowed is True

    assert gov.get_snapshot()["graph_build_requests_total"] == 0


def test_check_phase_boundary_none_governor_raises_value_error():
    with pytest.raises(ValueError):
        check_phase_boundary(None, GraphBuildPhase.ANALYZE)


def test_check_phase_boundary_none_phase_raises_value_error():
    gov = _gov_at_used_pct(5.0)
    with pytest.raises(ValueError):
        # Intentionally passing None to verify check_phase_boundary's own
        # runtime validation.
        check_phase_boundary(gov, None)  # type: ignore[arg-type]


def test_default_safety_factor_is_documented_and_positive():
    assert DEFAULT_SAFETY_FACTOR > 0
