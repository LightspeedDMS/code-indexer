"""Story #1787 AC12/AC13 (amendment): two-gate admission and phase-boundary
checks for the X-Ray whole-repository graph build.

Every admission DECISION goes through the EXISTING
`MemoryGovernor.admission_allowed()`/`.band` surface -- this module never
invents a parallel gating primitive (ADR-003 Decision 1). Rust's job is
producing the exact numbers these gates consume (`PreBindStats`, mirroring
`rust/xray-core/src/graph/bind/admission.rs`'s struct of the same name)
and enforcing the OS-level ceiling Gate 2's admitted estimate implies
(AC15, `analyze/memory_ceiling.rs`) -- Rust never decides admission.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from code_indexer.server.services.memory_governor import MemoryBand

from .k_seed_table import k_for_language
from .status import GraphBuildAbortStatus, GraphBuildPhase

logger = logging.getLogger(__name__)

# AC12: "convert to a required headroom percentage... a large build
# demands a LOWER watermark (more headroom); a small one passes a higher
# watermark." Bounds keep the watermark sane regardless of how extreme
# the estimate is (an estimate of 0 must not produce a 100% watermark
# that defeats the RED/first-sample fail-safe baked into
# admission_allowed(); a wildly oversized estimate must not produce a 0%
# watermark that can never admit anything).
_MIN_WATERMARK_PCT = 10.0
_MAX_WATERMARK_PCT = 80.0

# AC12: "estimated_peak_bytes = source_bytes * K(language) *
# safety_factor". A flat multiplicative margin covering estimation noise
# Gate 1 cannot otherwise see; Gate 2 recomputes precisely once real
# extraction counts exist, so it uses the SAME default for consistency
# rather than a second, independently-tuned constant.
DEFAULT_SAFETY_FACTOR = 1.5

# AC12 Gate 2 per-item byte weights, matching the REAL Rust CSR struct
# sizes this estimate is standing in for (see
# rust/xray-core/src/graph/csr/{candidate,reference}.rs and
# bind/name_index.rs's DeclInfo) -- kept as named constants rather than
# folded into one opaque multiplier so a future recalibration (AC16) can
# target the specific structure that changed size.
_BYTES_PER_CANDIDATE_EDGE = (
    8  # Candidate { symbol: u32, confidence: u8, reasons: u16 }, aligned
)
_BYTES_PER_CALL_SITE_REFERENCE = (
    24  # Reference { from, file, line, kind, cand_start, cand_len }, aligned
)
_BYTES_PER_DECLARATION_SYMBOL = (
    64  # symbol table + string table entry + binder scratch (DeclInfo clones)
)


@dataclass(frozen=True)
class PreBindStats:
    """Python mirror of Rust's `bind::admission::PreBindStats` (AC12 Gate
    2): exact counts computable before any CSR/candidate-arena
    allocation. The real orchestrator populates this from the extract
    subprocess's reported counts.
    """

    declaration_count: int
    call_site_count: int
    candidate_edge_count: int


@dataclass(frozen=True)
class AdmissionDecision:
    """Outcome of ONE admission check (Gate 1, Gate 2, or a phase
    boundary). `allowed=False` always carries the specific
    `GraphBuildAbortStatus` that explains why -- never inferred by the
    caller from an empty/falsy result (Rule 13, anti-silent-failure).
    """

    allowed: bool
    abort_status: Optional[GraphBuildAbortStatus] = None


def _require_not_none(value, name: str) -> None:
    if value is None:
        raise ValueError(f"{name} must not be None")


def _require_non_negative(value: int, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def _require_positive_safety_factor(safety_factor: float) -> None:
    if safety_factor <= 0:
        raise ValueError(f"safety_factor must be positive, got {safety_factor}")


def watermark_for(estimated_peak_bytes: int, cgroup_limit_bytes: int) -> float:
    """AC12: required headroom, expressed as the admission watermark
    `governor.admission_allowed(max_used_pct=...)` expects. A larger
    estimate (relative to the cgroup limit) produces a LOWER watermark
    (stricter -- more headroom demanded); a small estimate produces a
    HIGHER watermark (looser). Clamped to
    [`_MIN_WATERMARK_PCT`, `_MAX_WATERMARK_PCT`].
    """
    _require_non_negative(estimated_peak_bytes, "estimated_peak_bytes")
    if cgroup_limit_bytes <= 0:
        return _MIN_WATERMARK_PCT
    estimated_pct_of_limit = (estimated_peak_bytes / cgroup_limit_bytes) * 100.0
    watermark = 100.0 - estimated_pct_of_limit
    return max(_MIN_WATERMARK_PCT, min(_MAX_WATERMARK_PCT, watermark))


def estimate_gate1_bytes(
    source_bytes_by_language: Dict[str, int],
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
) -> int:
    """AC12 Gate 1: totals candidate-file BYTES per language (never file
    count -- "files averaged 4.4 KB with high variance, and bytes is
    strictly the better predictor"), applying each language's own K.
    """
    _require_not_none(source_bytes_by_language, "source_bytes_by_language")
    _require_positive_safety_factor(safety_factor)
    total = 0.0
    for language, source_bytes in source_bytes_by_language.items():
        _require_non_negative(source_bytes, f"source_bytes[{language!r}]")
        total += source_bytes * k_for_language(language) * safety_factor
    return int(total)


def estimate_gate2_bytes(
    pre_bind_stats: PreBindStats, safety_factor: float = DEFAULT_SAFETY_FACTOR
) -> int:
    """AC12 Gate 2: exact byte estimate from `PreBindStats` -- computed
    BEFORE the CSR candidate arena is allocated (the structural point
    AC12 exists to preserve; see `rust/xray-core/src/graph/bind/admission.rs`).
    """
    _require_not_none(pre_bind_stats, "pre_bind_stats")
    _require_positive_safety_factor(safety_factor)
    _require_non_negative(pre_bind_stats.declaration_count, "declaration_count")
    _require_non_negative(pre_bind_stats.call_site_count, "call_site_count")
    _require_non_negative(pre_bind_stats.candidate_edge_count, "candidate_edge_count")
    raw_bytes = (
        pre_bind_stats.candidate_edge_count * _BYTES_PER_CANDIDATE_EDGE
        + pre_bind_stats.call_site_count * _BYTES_PER_CALL_SITE_REFERENCE
        + pre_bind_stats.declaration_count * _BYTES_PER_DECLARATION_SYMBOL
    )
    return int(raw_bytes * safety_factor)


def _check_gate(
    governor,
    gate_name: str,
    estimated_peak_bytes: int,
    cgroup_limit_bytes: int,
    denied_status: GraphBuildAbortStatus,
) -> AdmissionDecision:
    """Shared admission/record/decision logic for Gate 1 and Gate 2: both
    gates differ only in HOW `estimated_peak_bytes` was computed -- the
    watermark conversion, `admission_allowed()` call, and AC17 recording
    are otherwise identical.
    """
    watermark = watermark_for(estimated_peak_bytes, cgroup_limit_bytes)
    if governor.admission_allowed(max_used_pct=watermark):
        governor.record_graph_build_outcome(estimated_peak_bytes=estimated_peak_bytes)
        return AdmissionDecision(allowed=True)
    governor.record_graph_build_outcome(
        denied_gate=gate_name, estimated_peak_bytes=estimated_peak_bytes
    )
    return AdmissionDecision(allowed=False, abort_status=denied_status)


def check_gate1(
    governor,
    source_bytes_by_language: Dict[str, int],
    cgroup_limit_bytes: int,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
) -> AdmissionDecision:
    """AC12 Gate 1: coarse, pre-extract admission check -- BEFORE
    extraction runs at all.
    """
    _require_not_none(governor, "governor")
    estimated_peak_bytes = estimate_gate1_bytes(source_bytes_by_language, safety_factor)
    return _check_gate(
        governor,
        "gate1",
        estimated_peak_bytes,
        cgroup_limit_bytes,
        GraphBuildAbortStatus.ADMISSION_DENIED_GATE1,
    )


def check_gate2(
    governor,
    pre_bind_stats: PreBindStats,
    cgroup_limit_bytes: int,
    safety_factor: float = DEFAULT_SAFETY_FACTOR,
) -> AdmissionDecision:
    """AC12 Gate 2: exact, post-extract admission check -- called with
    `PreBindStats` from the extract phase's exact counts, BEFORE the
    caller invokes the (separate, per ADR-003 Decision 2) allocation step.
    """
    _require_not_none(governor, "governor")
    estimated_peak_bytes = estimate_gate2_bytes(pre_bind_stats, safety_factor)
    return _check_gate(
        governor,
        "gate2",
        estimated_peak_bytes,
        cgroup_limit_bytes,
        GraphBuildAbortStatus.ADMISSION_DENIED_GATE2,
    )


def check_phase_boundary(governor, phase: GraphBuildPhase) -> AdmissionDecision:
    """AC13: checked at each phase boundary (extract -> bind -> analyze
    -> refine). On RED, aborts with `ABORTED_MEMORY_PRESSURE` -- NEVER a
    degrade-by-dropping-edges (AC6 already forbids it; a thinned graph
    produces confidently wrong "no reference"/"no auth path" verdicts).
    `phase` identifies WHICH boundary tripped -- logged on abort so an
    operator can see where in the pipeline pressure hit.

    Deliberately does NOT call `governor.record_graph_build_outcome` on
    the successful (non-RED) path: this boundary is checked up to 4 times
    per single build (once per phase), so counting every PASS would
    inflate `graph_build_requests_total` -- that counter's "how many
    builds were requested" semantics belongs to the once-per-build Gate
    1/Gate 2 checks. Only the RED-abort path is recorded, matching AC17's
    literal ask ("Gate-2 aborts, and RED aborts").
    """
    _require_not_none(governor, "governor")
    _require_not_none(phase, "phase")
    if governor.band == MemoryBand.RED:
        logger.warning(
            "X-Ray graph build aborted at phase boundary %s: governor band is RED",
            phase.value,
        )
        governor.record_graph_build_outcome(red_abort=True)
        return AdmissionDecision(
            allowed=False, abort_status=GraphBuildAbortStatus.ABORTED_MEMORY_PRESSURE
        )
    return AdmissionDecision(allowed=True)
