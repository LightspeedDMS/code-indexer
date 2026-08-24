"""One semantic-backfill cycle = discover golden repos missing their semantic vector
index and ENQUEUE a bounded, cost-gated re-embed for each.

Thin adapter over the shared :class:`GoldenIndexBackfillReconciler` (the DRY engine
used by FTS + SCIP) + :class:`SemanticBackfillPolicy`. Inert when disabled (the policy's
eligibility returns None), so calling this with the sweep disabled enqueues nothing.
"""

from __future__ import annotations

from typing import Any

from ..golden_index_backfill import BackfillCounts, GoldenIndexBackfillReconciler
from .policy import SemanticBackfillPolicy


def run_semantic_backfill_cycle(
    golden_repo_manager: Any,
    background_job_manager: Any,
    *,
    enabled: bool,
    batch_size: int,
    cooldown_seconds: int,
) -> BackfillCounts:
    """Enqueue up to ``batch_size`` per-repo semantic re-embeds for golden repos whose
    vector index is missing. Idempotent + cursor-free: each cycle re-scans, so a repo
    whose re-embed lands drops out and the sweep converges. ``enabled=False`` (default)
    makes it a no-op — the cost gate lives in the policy's eligibility."""
    reconciler = GoldenIndexBackfillReconciler(golden_repo_manager.golden_repos_dir)
    policy = SemanticBackfillPolicy(
        golden_repo_manager, background_job_manager, enabled=enabled
    )
    return reconciler.run_cycle(
        policy, max_enqueue=batch_size, cooldown_seconds=cooldown_seconds
    )
