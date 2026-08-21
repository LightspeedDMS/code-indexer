"""EVO-66445: one FTS backfill cycle = discover missing-FTS golden repos and ENQUEUE
a per-repo build for each (bounded).

This is now a thin adapter over the shared :class:`GoldenIndexBackfillReconciler`
(the DRY engine used by SCIP too) + the :class:`FtsBackfillPolicy`. The leader runs
this cheap discovery/enqueue tick; the enqueued ``fts_backfill_build`` jobs are
POD_PULL cross-pod-claimable, so every pod builds DISTINCT repos in parallel — no
collisions, full cluster utilization (replacing the old leader-serial batch build).
"""

from __future__ import annotations

from typing import Any

from ..golden_index_backfill import BackfillCounts, GoldenIndexBackfillReconciler
from .policy import FtsBackfillPolicy


def run_fts_backfill_cycle(
    golden_repo_manager: Any,
    background_job_manager: Any,
    *,
    batch_size: int,
    cooldown_seconds: int,
) -> BackfillCounts:
    """Enqueue up to ``batch_size`` per-repo FTS builds for golden repos missing FTS.

    Idempotent + resumable with no cursor: each cycle re-scans, so built repos drop
    out and the sweep converges. Enqueue is single-flight per repo (DuplicateJobError),
    so a repo already building is never double-enqueued.
    """
    reconciler = GoldenIndexBackfillReconciler(golden_repo_manager.golden_repos_dir)
    policy = FtsBackfillPolicy(background_job_manager)
    return reconciler.run_cycle(
        policy, max_enqueue=batch_size, cooldown_seconds=cooldown_seconds
    )
