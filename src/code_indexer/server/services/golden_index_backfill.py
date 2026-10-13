"""EVO-66445: one DRY engine for golden-repo index backfill.

Several index types (SCIP, FTS, temporal later) share the SAME reconcile shape: the
leader periodically scans the golden-repos dir, finds repos MISSING index X that are
ELIGIBLE for it, and ENQUEUES a per-repo build — throttled by a per-repo cooldown
marker (a failed build must not re-fire every cycle) and bounded per cycle (no
thundering herd over hundreds of repos).

Only two things vary, and they are the whole of the per-type policy:
  * index policy   — is it present? / is it eligible? / how do you enqueue one repo?
  * enqueue backend — SCIP funnels to its serialized DELEGATED sandbox queue; FTS
    submits a POD_PULL cross-pod-claimable job so every pod builds distinct repos in
    parallel (FTS is a cheap LOCAL tantivy build, no shared-sandbox bottleneck).

:class:`GoldenIndexBackfillReconciler.run_cycle` owns the loop, the cooldown marker
and the bound exactly ONCE; each :class:`IndexBackfillPolicy` supplies the three
varying hooks. Leader-gating stays in the caller (the lifespan loop), unchanged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class BackfillCounts:
    """Per-cycle outcome tally (returned + logged). Generic across index types."""

    scanned: int = 0
    present: int = 0  # already indexed — nothing to do
    not_eligible: int = 0  # no discoverable/indexable project for this type
    cooldown_skipped: int = 0  # attempted within the cooldown window — retried later
    enqueued: int = 0  # a per-repo build was enqueued this cycle
    errors: int = 0  # per-repo failure (never aborts the cycle)

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


@runtime_checkable
class IndexBackfillPolicy(Protocol):
    """The per-index-type strategy the engine drives. Small + pure of loop mechanics."""

    #: short label for logs (e.g. "scip", "fts").
    name: str
    #: cooldown-attempt marker filename under ``<repo>/.code-indexer/`` (kept per type so
    #: adopting the engine never resets an existing type's cooldowns).
    attempt_marker: str

    def is_present(self, repo_path: Path) -> bool:
        """True iff ``repo_path`` already has a BUILT index of this type.

        Existence only. Whether that index is still CURRENT is decided by the
        engine via :meth:`index_dir`, so no policy has to remember the check.
        """
        ...

    def is_fresh(self, repo_path: Path) -> bool:
        """True iff an existing index still matches the clone it was built from.

        Separate from :meth:`is_present` because existence and currency are
        different questions, and conflating them is what let an index drift
        arbitrarily far behind its clone. Policies answer with their own rules --
        SCIP layers a no-index token and a DB-shape check on the shared
        ``.build_commit`` convention -- and a policy that cannot be rebuilt cheaply
        returns ``True`` to opt out.
        """
        ...

    def eligibility(self, repo_path: Path) -> Optional[Any]:
        """Return per-repo enqueue metadata when the repo is eligible for this index,
        else ``None`` (skip). The returned value is opaque to the engine and handed
        straight back to :meth:`enqueue`."""
        ...

    def enqueue(self, alias: str, repo_path: Path, meta: Any) -> None:
        """Enqueue ONE per-repo build (funnel/submit_job). Backend is the policy's
        concern — the engine only counts the enqueue."""
        ...


def _index_is_outdated(policy: "IndexBackfillPolicy", repo_path: Path) -> bool:
    """True when this policy's index exists but no longer matches the clone.

    Applied in the engine, not repeated in each policy, for the same reason the
    cooldown and the per-cycle bound are: it is loop mechanics every index type
    needs, and a policy that has to remember it is one that will eventually forget.
    """
    try:
        return not policy.is_fresh(repo_path)
    except Exception as exc:  # noqa: BLE001 - a policy hook must never abort the sweep
        logger.warning(
            "golden_index_backfill[%s]: freshness check failed for %s: %s",
            getattr(policy, "name", "?"),
            repo_path,
            exc,
        )
        return False


def _cooldown_active(marker: Path, cooldown_seconds: int, now: float) -> bool:
    """True iff ``marker`` was touched within ``cooldown_seconds`` of ``now``.
    Fail-open (False) on any stat error — a marker glitch must not wedge a repo."""
    if cooldown_seconds <= 0:
        return False
    try:
        return marker.exists() and (now - marker.stat().st_mtime) < cooldown_seconds
    except OSError:
        return False


class GoldenIndexBackfillReconciler:
    """Scan the golden-repos dir and enqueue a per-repo build for repos missing index X.

    One implementation of the loop, the cooldown marker and the per-cycle bound; the
    per-type :class:`IndexBackfillPolicy` supplies present/eligible/enqueue.
    """

    def __init__(
        self, golden_repos_dir: str | Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._root = Path(golden_repos_dir)
        self._clock = clock

    def run_cycle(
        self,
        policy: IndexBackfillPolicy,
        *,
        max_enqueue: int,
        cooldown_seconds: int,
    ) -> BackfillCounts:
        """Enqueue up to ``max_enqueue`` per-repo builds for repos missing this index.

        Idempotent + resumable WITHOUT a durable cursor: each cycle re-scans, so
        already-built repos drop out and the sweep converges. A single bad repo is
        counted and logged but never aborts the cycle.
        """
        counts = BackfillCounts()
        if not self._root.is_dir():
            return counts
        now = self._clock()
        for entry in sorted(self._root.iterdir(), key=lambda p: p.name):
            if counts.enqueued >= max_enqueue:
                break
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            counts.scanned += 1
            try:
                if policy.is_present(entry) and not _index_is_outdated(policy, entry):
                    counts.present += 1
                    continue
                # Cooldown is checked BEFORE eligibility (which may walk NFS), so a
                # recently-attempted repo isn't re-walked every cycle only to be skipped.
                marker = entry / ".code-indexer" / policy.attempt_marker
                if _cooldown_active(marker, cooldown_seconds, now):
                    counts.cooldown_skipped += 1
                    continue
                meta = policy.eligibility(entry)
                if meta is None:
                    counts.not_eligible += 1
                    continue
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
                policy.enqueue(entry.name, entry, meta)
                counts.enqueued += 1
                logger.info(
                    "golden_index_backfill[%s]: enqueued build for '%s'",
                    policy.name,
                    entry.name,
                )
            except Exception as exc:  # noqa: BLE001 — one bad repo never aborts the cycle
                counts.errors += 1
                logger.warning(
                    "golden_index_backfill[%s]: reconcile failed for '%s': %s",
                    policy.name,
                    entry.name,
                    exc,
                )
        return counts
