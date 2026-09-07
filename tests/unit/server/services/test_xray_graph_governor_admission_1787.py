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
from code_indexer.server.services.xray_graph_governor.k_seed_table import (
    k_for_language,
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
_JAVA_K = k_for_language("java")

# H5 fixture constants: a node that is nearly idle (LOW) vs. one that is
# genuinely busy (HIGH) relative to the min required 10% admission
# headroom -- used to discriminate "cannot possibly fit" (denied even at
# LOW contention) from "could fit, but the node is too busy right now"
# (denied only at HIGH contention).
_LOW_CONTENTION_USED_PCT = 8.0
_HIGH_CONTENTION_USED_PCT = 75.0
# A build whose estimate is this FRACTION of the cgroup limit leaves real
# headroom (well above the 10% floor) -- it structurally COULD fit.
_ESTIMATE_FRACTION_WITH_REAL_HEADROOM = 0.30
_HEADROOM_LIMIT_BYTES = 100 * _ONE_GIB
# A cgroup limit small enough that even a modest, realistic estimate
# exceeds it outright -- the exact "cannot possibly fit" shape H5 fixes.
_OVERSIZED_BUILD_LIMIT_BYTES = 2 * _ONE_GIB
# The dual review's own Elasticsearch-scenario number: 135.8 MB * K 19.0 *
# safety factor 1.5 =~ 3.87 GB against a 2 GB cgroup limit.
_REVIEW_EXAMPLE_ESTIMATED_GIB = 3.87
# A target estimate this many TIMES _OVERSIZED_BUILD_LIMIT_BYTES -- always
# outright oversized (> 100% of the limit) regardless of safety factor.
_OVERSIZED_ESTIMATE_MULTIPLIER = 1.5
# Mirrors estimate_gate2_bytes' own documented per-candidate-edge weight.
_BYTES_PER_CANDIDATE_EDGE = 8


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
    # 90 GiB estimate against a 100 GiB limit leaves exactly the minimum
    # required 10% headroom -- still admissible in principle (contention
    # decides), never an outright "cannot fit" denial.
    watermark = watermark_for(
        estimated_peak_bytes=90 * _ONE_GIB, cgroup_limit_bytes=100 * _ONE_GIB
    )
    assert watermark == pytest.approx(10.0)  # clamped at the min watermark


def test_h5_estimate_leaving_less_than_minimum_headroom_returns_none_never_clamped_up():
    """Dual-review defect H5 (High): an estimate that would leave LESS than
    the minimum required headroom must return None (an outright "cannot
    admit" signal) -- it must NEVER be clamped up to `_MIN_WATERMARK_PCT`
    and treated as if 10% headroom were actually available. 95 GiB against
    a 100 GiB limit leaves only 5% headroom, below the 10% floor."""
    watermark = watermark_for(
        estimated_peak_bytes=95 * _ONE_GIB, cgroup_limit_bytes=100 * _ONE_GIB
    )
    assert watermark is None


def test_h5_estimate_exceeding_the_cgroup_limit_outright_returns_none():
    """THE exact dual-review scenario: an estimate (3.87 GB) that exceeds
    the cgroup limit (2 GB) outright -- literally cannot fit regardless of
    current contention -- must return None, never a numeric watermark a
    caller could accidentally admit against."""
    watermark = watermark_for(
        estimated_peak_bytes=int(3.87 * _ONE_GIB), cgroup_limit_bytes=2 * _ONE_GIB
    )
    assert watermark is None


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
    # Estimate = _ESTIMATE_FRACTION_WITH_REAL_HEADROOM of the cgroup limit
    # -> watermark = 70% (well above the H5 outright-deny floor of 10%,
    # so this build structurally COULD fit) -- but the governor's REAL
    # usage (_HIGH_CONTENTION_USED_PCT) still exceeds that watermark, a
    # true "node is too busy right now" denial, distinct from H5's "this
    # build cannot possibly fit" denial.
    gov = _gov_at_used_pct(_HIGH_CONTENTION_USED_PCT)
    source_bytes = int(
        (_ESTIMATE_FRACTION_WITH_REAL_HEADROOM * _HEADROOM_LIMIT_BYTES)
        / (_JAVA_K * DEFAULT_SAFETY_FACTOR)
    )

    decision = check_gate1(
        gov, {"java": source_bytes}, cgroup_limit_bytes=_HEADROOM_LIMIT_BYTES
    )

    assert decision.allowed is False
    assert decision.abort_status == GraphBuildAbortStatus.ADMISSION_DENIED_GATE1
    assert gov.get_snapshot()["graph_gate1_denials"] == 1


def test_check_gate1_none_governor_raises_value_error():
    with pytest.raises(ValueError):
        check_gate1(None, {"java": 1}, cgroup_limit_bytes=1)


def test_h5_check_gate1_denies_outright_when_estimate_exceeds_limit_even_at_low_contention():
    """THE exact dual-review H5 scenario: an Elasticsearch-class estimate
    (135.8 MB * K 19.0 * 1.5 =~ 3.87 GB) against a 2 GB cgroup limit, on a
    node at only 8% usage. The pre-fix clamp-to-10%-watermark bug would
    ADMIT this (8% < 10%) even though the build cannot possibly fit --
    the fix must deny outright with the distinct H5 status, never the
    plain contention-based ADMISSION_DENIED_GATE1."""
    gov = _gov_at_used_pct(_LOW_CONTENTION_USED_PCT)  # node is nearly idle
    source_bytes_for_review_example = int(
        (_REVIEW_EXAMPLE_ESTIMATED_GIB * _ONE_GIB) / (_JAVA_K * DEFAULT_SAFETY_FACTOR)
    )

    decision = check_gate1(
        gov,
        {"java": source_bytes_for_review_example},
        cgroup_limit_bytes=_OVERSIZED_BUILD_LIMIT_BYTES,
    )

    assert decision.allowed is False, (
        "a build that cannot fit must never be admitted just because the node is idle"
    )
    assert (
        decision.abort_status
        == GraphBuildAbortStatus.ADMISSION_DENIED_ESTIMATE_EXCEEDS_LIMIT
    )
    assert gov.get_snapshot()["graph_gate1_denials"] == 1


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
    # Same shape as the Gate 1 counterpart above: an estimate that leaves
    # real (_ESTIMATE_FRACTION_WITH_REAL_HEADROOM) headroom under the
    # limit -- structurally COULD fit -- but the governor's real usage
    # (_HIGH_CONTENTION_USED_PCT) still exceeds that watermark.
    gov = _gov_at_used_pct(_HIGH_CONTENTION_USED_PCT)
    raw_bytes_target = (
        _ESTIMATE_FRACTION_WITH_REAL_HEADROOM * _HEADROOM_LIMIT_BYTES
    ) / DEFAULT_SAFETY_FACTOR
    candidate_edge_count = int(raw_bytes_target / _BYTES_PER_CANDIDATE_EDGE)
    stats = PreBindStats(
        declaration_count=0,
        call_site_count=0,
        candidate_edge_count=candidate_edge_count,
    )

    decision = check_gate2(gov, stats, cgroup_limit_bytes=_HEADROOM_LIMIT_BYTES)

    assert decision.allowed is False
    assert decision.abort_status == GraphBuildAbortStatus.ADMISSION_DENIED_GATE2
    snapshot = gov.get_snapshot()
    assert snapshot["graph_gate2_denials"] == 1
    assert snapshot["graph_gate1_denials"] == 0, (
        "gate2 denial must not be miscounted as a gate1 denial"
    )


def test_h5_check_gate2_denies_outright_when_estimate_exceeds_limit_even_at_low_contention():
    """Gate 2 counterpart of the Gate 1 H5 test above: an exact post-extract
    estimate that exceeds the cgroup limit must be denied outright with
    ADMISSION_DENIED_ESTIMATE_EXCEEDS_LIMIT, even at low node contention
    where the pre-fix clamp-to-10% bug would have admitted it."""
    gov = _gov_at_used_pct(_LOW_CONTENTION_USED_PCT)
    target_estimated_peak_bytes = (
        _OVERSIZED_ESTIMATE_MULTIPLIER * _OVERSIZED_BUILD_LIMIT_BYTES
    )
    oversized_raw_bytes = target_estimated_peak_bytes / DEFAULT_SAFETY_FACTOR
    candidate_edge_count = int(oversized_raw_bytes / _BYTES_PER_CANDIDATE_EDGE)
    stats = PreBindStats(
        declaration_count=0,
        call_site_count=0,
        candidate_edge_count=candidate_edge_count,
    )

    decision = check_gate2(gov, stats, cgroup_limit_bytes=_OVERSIZED_BUILD_LIMIT_BYTES)

    assert decision.allowed is False
    assert (
        decision.abort_status
        == GraphBuildAbortStatus.ADMISSION_DENIED_ESTIMATE_EXCEEDS_LIMIT
    )
    assert gov.get_snapshot()["graph_gate2_denials"] == 1


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
