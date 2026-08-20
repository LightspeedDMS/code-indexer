"""EVO-66445: one paced tick of the golden-repo FTS backfill sweep."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ..fts_index_builder import FtsBuildStatus, ensure_fts_index
from .discovery import FtsBackfillCandidate, discover_repos_missing_fts

logger = logging.getLogger(__name__)


@dataclass
class FtsBackfillTickStats:
    """Per-tick counters for the FTS backfill sweep."""

    total_missing: int = 0  # golden repos discovered missing FTS at tick start
    attempted: int = 0  # repos we tried to build this tick (<= batch_size)
    rebuilt: int = 0  # successful (re)builds this tick
    failed: int = 0  # build failures this tick (surfaced, not swallowed)
    failed_aliases: List[str] = field(default_factory=list)

    @property
    def remaining_after_tick(self) -> int:
        """Best-effort estimate of repos still missing FTS after this tick."""
        return max(0, self.total_missing - self.rebuilt)

    @property
    def complete(self) -> bool:
        """True when the fleet had nothing left to build at tick start."""
        return self.total_missing == 0


def run_fts_backfill_tick(
    golden_repo_manager: Any,
    *,
    batch_size: int,
    subprocess_env: Optional[dict] = None,
    ensure_fn: Callable[..., Any] = ensure_fts_index,
    discover_fn: Callable[[Any], List[FtsBackfillCandidate]] = discover_repos_missing_fts,
    timeout: Optional[int] = None,
) -> FtsBackfillTickStats:
    """Build FTS for up to ``batch_size`` golden repos that currently lack it.

    Idempotent and resumable WITHOUT a durable cursor: each tick re-discovers repos
    missing FTS, so already-built repos drop out naturally and the sweep converges
    to a cheap no-op once the fleet is complete. Per-repo failures are counted and
    logged loudly but never abort the tick. The FTS rebuild is FTS-only, so it
    performs no embedding calls (no VoyageAI cost).

    Returns the per-tick stats (also used for dashboard/health surfacing).
    """
    candidates = discover_fn(golden_repo_manager)
    stats = FtsBackfillTickStats(total_missing=len(candidates))
    if not candidates:
        logger.info("fts_backfill tick: no golden repos missing FTS (fleet complete)")
        return stats

    for cand in candidates[: max(0, batch_size)]:
        stats.attempted += 1
        outcome = ensure_fn(
            cand.repo_path,
            alias=cand.alias,
            subprocess_env=subprocess_env,
            timeout=timeout,
        )
        if outcome.status is FtsBuildStatus.FAILED:
            stats.failed += 1
            stats.failed_aliases.append(cand.alias)
        else:
            stats.rebuilt += 1

    logger.info(
        "fts_backfill tick: missing=%d attempted=%d rebuilt=%d failed=%d "
        "remaining~=%d%s",
        stats.total_missing,
        stats.attempted,
        stats.rebuilt,
        stats.failed,
        stats.remaining_after_tick,
        f" failed_aliases={stats.failed_aliases}" if stats.failed_aliases else "",
    )
    return stats
