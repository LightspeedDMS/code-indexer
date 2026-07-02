"""
ProgressPhaseAllocator: Dynamic phase weight calculation for index rebuild jobs.

Story #480: Real-Time Progress Reporting for Index Rebuild Jobs.

Maps per-phase local progress (0..total) into a global 0-100 progress range.
Each phase receives a proportional slice of 0-100 based on estimated cost.

Cost model:
  semantic  : file_count * COST_PER_FILE
  temporal  : min(commit_count, max_commits) * COST_PER_COMMIT
  fts       : COST_FTS_FIXED (fast, fixed)
  scip      : COST_SCIP_FIXED (fast, fixed)
  cow       : COST_COW_FIXED  (always last, copy-on-write snapshot)

Execution order (canonical): semantic → fts → temporal → scip → cow
Only phases in the requested index_types list are included (plus cow always).
"""

import json
from dataclasses import dataclass
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Cost constants (as specified in Story #480)
# ---------------------------------------------------------------------------

COST_PER_FILE: float = 1.0
COST_PER_COMMIT: float = 2.5
COST_FTS_FIXED: float = 50.0
COST_SCIP_FIXED: float = 30.0
COST_COW_FIXED: float = 20.0

# Canonical execution order for multi-phase jobs
_EXECUTION_ORDER: List[str] = ["semantic", "fts", "temporal", "scip", "cow"]


# ---------------------------------------------------------------------------
# Phase dataclass
# ---------------------------------------------------------------------------


@dataclass
class Phase:
    """
    Represents a single indexing phase with its global progress range.

    Attributes:
        name        : Phase identifier (e.g., "semantic", "temporal", "cow")
        weight      : Fraction of total work this phase represents (0.0–1.0)
        range_start : Global progress value at which this phase begins (0–100)
        range_end   : Global progress value at which this phase ends   (0–100)
    """

    name: str
    weight: float
    range_start: float
    range_end: float


# ---------------------------------------------------------------------------
# ProgressPhaseAllocator
# ---------------------------------------------------------------------------


class ProgressPhaseAllocator:
    """
    Dynamically allocates progress ranges to indexing phases.

    Usage::

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic", "temporal"],
            file_count=500,
            commit_count=200,
        )
        # Map 50/100 files in the semantic phase to a global value:
        global_pct = allocator.map_phase_progress("semantic", 50, 100)
        # → e.g., 38.5 (depends on weights)

    The CoW snapshot phase is always appended last, regardless of
    whether "cow" is included in index_types.
    """

    def __init__(self) -> None:
        self.phases: List[Phase] = []
        self._phase_lookup: Dict[str, Phase] = {}

    def calculate_weights(
        self,
        index_types: List[str],
        file_count: int,
        commit_count: int,
        max_commits: Optional[int] = None,
    ) -> None:
        """
        Calculate phase weights based on repo characteristics.

        Builds self.phases in canonical execution order.  "cow" is always
        appended as the final phase.

        Args:
            index_types  : List of index types to process
                           (subset of "semantic", "fts", "temporal", "scip")
            file_count   : Number of tracked files in the repository
            commit_count : Total number of git commits
            max_commits  : If set, caps the effective commit count used for
                           temporal cost estimation
        """
        self.phases = []
        self._phase_lookup = {}

        # Build cost map
        cost_map: Dict[str, float] = {}

        if "semantic" in index_types:
            cost_map["semantic"] = float(file_count) * COST_PER_FILE

        if "fts" in index_types:
            cost_map["fts"] = COST_FTS_FIXED

        if "temporal" in index_types:
            effective_commits = (
                min(commit_count, max_commits)
                if max_commits is not None
                else commit_count
            )
            cost_map["temporal"] = float(effective_commits) * COST_PER_COMMIT

        if "scip" in index_types:
            cost_map["scip"] = COST_SCIP_FIXED

        # CoW is always present
        cost_map["cow"] = COST_COW_FIXED

        total_cost = sum(cost_map.values())

        # Build phases in canonical execution order
        range_cursor = 0.0
        for phase_name in _EXECUTION_ORDER:
            if phase_name not in cost_map:
                continue
            weight = cost_map[phase_name] / total_cost
            range_start = range_cursor
            range_end = range_cursor + (weight * 100.0)
            phase = Phase(
                name=phase_name,
                weight=weight,
                range_start=range_start,
                range_end=range_end,
            )
            self.phases.append(phase)
            self._phase_lookup[phase_name] = phase
            range_cursor = range_end

        # Clamp last phase to exactly 100.0 to avoid floating-point drift
        if self.phases:
            self.phases[-1].range_end = 100.0

    def _get_phase(self, phase_name: str) -> Phase:
        """Return the Phase for phase_name, raising ValueError if not found."""
        phase = self._phase_lookup.get(phase_name)
        if phase is None:
            known = list(self._phase_lookup.keys())
            raise ValueError(
                f"Phase '{phase_name}' not found in allocator. "
                f"Known phases: {known}. "
                f"Did you call calculate_weights() first?"
            )
        return phase

    def map_phase_progress(
        self, phase_name: str, local_current: int, local_total: int
    ) -> float:
        """
        Map phase-local progress to a global 0–100 value.

        Args:
            phase_name    : Name of the phase (must be in self._phase_lookup)
            local_current : Current step within the phase
            local_total   : Total steps for the phase

        Returns:
            Global progress value in the range [phase.range_start, phase.range_end]

        Raises:
            ValueError: If phase_name is unknown
        """
        phase = self._get_phase(phase_name)
        if local_total == 0:
            return float(phase.range_start)
        local_fraction = float(local_current) / float(local_total)
        return phase.range_start + local_fraction * (
            phase.range_end - phase.range_start
        )

    def phase_start(self, phase_name: str) -> float:
        """
        Return the global progress value at the start of a phase.

        Useful for reporting coarse markers (FTS, SCIP, CoW).

        Raises:
            ValueError: If phase_name is unknown
        """
        return float(self._get_phase(phase_name).range_start)

    def phase_end(self, phase_name: str) -> float:
        """
        Return the global progress value at the end of a phase.

        Useful for reporting coarse markers (FTS, SCIP, CoW).

        Raises:
            ValueError: If phase_name is unknown
        """
        return float(self._get_phase(phase_name).range_end)


# ---------------------------------------------------------------------------
# JSON progress line helpers
# ---------------------------------------------------------------------------


def parse_progress_line(line: str) -> Optional[Dict]:
    """
    Parse a single stdout line as a JSON progress update.

    Returns a dict with at least "current" and "total" keys on success.
    Returns None for any non-JSON line, malformed JSON, or missing required
    fields — enabling callers to safely skip non-progress output.

    Args:
        line: A single line of text from subprocess stdout

    Returns:
        Dict with keys "current", "total", "info" on success, None otherwise.
    """
    stripped = line.strip()
    if not stripped:
        return None

    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # Require "current" and "total" fields
    if "current" not in data or "total" not in data:
        return None

    return {
        "current": data["current"],
        "total": data["total"],
        "info": data.get("info", ""),
    }


def emit_progress_json(current: int, total: int, info: str = "") -> None:
    """
    Emit a single JSON progress line to stdout.

    Called by the CLI when --progress-json flag is active.
    Flushes immediately so the parent process can read it line-by-line.

    Args:
        current : Current step count
        total   : Total step count
        info    : Human-readable progress description
    """
    payload = {"current": current, "total": total, "info": info}
    print(json.dumps(payload), flush=True)
