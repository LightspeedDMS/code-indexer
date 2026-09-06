"""Story #1787 AC12/AC13/AC15: distinct terminal statuses for the X-Ray
graph-build memory-governor integration.

Rule 13 (anti-silent-failure): every abort path a graph build can take
gets its OWN named status here -- never inferred from an empty findings
list or a bare boolean. AC17's observability requirement ("distinguish
'no graph queries were requested' from 'every graph query was denied
admission'") depends directly on these being distinct and always
attached to the outcome that produced them.
"""

from __future__ import annotations

from enum import Enum


class GraphBuildAbortStatus(Enum):
    """Every reason a graph build can terminate WITHOUT producing a graph,
    per the amendment's AC12/AC13/AC15.

    - ADMISSION_DENIED_GATE1: AC12 Gate 1 (coarse, pre-extract byte
      estimate) refused admission before extraction even started.
    - ADMISSION_DENIED_GATE2: AC12 Gate 2 (exact, post-extract counts)
      refused admission after extraction produced exact
      declaration/call-site/candidate-edge counts, BEFORE the CSR
      candidate arena was allocated.
    - ABORTED_MEMORY_PRESSURE: AC13 -- a phase-boundary check
      (extract -> bind -> analyze -> refine) observed
      `governor.band == RED` and aborted. Any per-file findings already
      produced are preserved; the result is marked incomplete.
    - ABORTED_MEMORY_LIMIT: AC15 -- the OS-level per-process cgroup
      `memory.max` ceiling on the analyze/graph-build child was
      genuinely exceeded and the kernel OOM-killed it.
    """

    ADMISSION_DENIED_GATE1 = "admission_denied_gate1"
    ADMISSION_DENIED_GATE2 = "admission_denied_gate2"
    ABORTED_MEMORY_PRESSURE = "aborted_memory_pressure"
    ABORTED_MEMORY_LIMIT = "aborted_memory_limit"


class GraphBuildPhase(Enum):
    """AC13's four phase boundaries at which `governor.band` is checked.

    Named EXACTLY as the story text orders them: "extract -> bind ->
    analyze -> refine". `refine` is S3 scope per this story's Non-Goals,
    but the boundary check itself is defined here now so S3 does not need
    to re-derive this enum.
    """

    EXTRACT = "extract"
    BIND = "bind"
    ANALYZE = "analyze"
    REFINE = "refine"
