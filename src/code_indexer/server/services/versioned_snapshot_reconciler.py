"""
Versioned-snapshot orphan sweep (Bug #1567).

CleanupManager's durable queue (cleanup_manager.py) stops FUTURE leaks but
cannot heal an EXISTING backlog (229 snapshots for one repo, keep-last-3,
~120GB, confirmed live). This module enumerates `.versioned/{namespace}/v_*`
(namespace = bare repo alias -- VersionedSnapshotManager.list_snapshots
already strips any "-global" suffix) and, per namespace, computes which
snapshots are PROVABLY superseded.

ALGORITHM (Codex-hardened round 2 -- binding, do not weaken)
--------------------------------------------------------------
Round 1 (`keep = target ∪ previous ∪ N-newest`) had a LIVE-DATA-DELETING
hole: `_create_snapshot` materializes a snapshot at its FINAL v_<ts> path,
validates it, and only THEN calls `swap_alias` -- so a snapshot newer than
the current target can exist on disk, referenced by nothing, for real.
"N newest" protected that in-flight build only by coincidence; enough
crash-orphans and the in-flight build itself becomes deletable. Fix:
anchor strictly BELOW the live target's own version id (`ts_live`), never
relative to "youngest N on disk":

    ts_live = int parsed from the governing pointer's target_path
              (must be v_<ts> literally under .versioned/{ns}/ -- a
              master clone, write-mode source, or foreign path means
              "cannot interpret, skip this namespace entirely")
    older   = {(p, ts) in list_snapshots(ns) : ts < ts_live}, sorted asc
    keep    = every target_path/previous_path from EVERY alias pointer,
              anywhere
            ∪ {p : ts >= ts_live}              # NEVER touched here
            ∪ newest (keep_last - 1) of older   # ts_live's own snapshot
                                                  # is the 1st of N kept
    delete  = (older \\ keep) filtered to age (by creation ts, own axis
              from CleanupManager's scheduled-at floor) >= min_absolute_age

SAFETY-DIRECTIONAL STALENESS: a stale read (NFS cache, a lagging node)
can only return an OLDER target_path -- nothing reads a pointer "from the
future". Excluding everything with ts >= ts_live unconditionally means
staleness can only ever UNDER-delete. Crash-orphans NEWER than target are
a SEPARATE leak class, deliberately not folded in (indistinguishable from
an in-flight publish by disk state alone).

THREE POINTER-READING HOLES CLOSED:
  A. NEVER AliasManager.read_alias()/get_previous_path() -- read_alias
     applies WRITE-MODE SOURCE redirection for an active write session on
     a `-global` alias, silently excluding the real live snapshot from
     the keep set. Pointer files are read directly here instead.
  B. SINGLE ATOMIC READ: one open+json.load per pointer, target_path AND
     previous_path from the SAME dict -- the pre-existing
     enforce_snapshot_retention read them via two separate opens, which
     can straddle a concurrent swap. Both now share
     read_pointer_target_and_previous() -- ONE "read a pointer" impl.
  C. UNION OVER EVERY `*.json` in aliases/, not just `*-global.json` --
     temporal sister pointers reference snapshots in their own
     namespaces; the mechanism that refreshed them is RETIRED
     (Bug #1528/#1529) but the pointer files still exist and must still
     protect their targets. namespace != alias 1:1: a `.versioned/{ns}`
     dir maps to `{ns}-global.json` OR `{ns}.json`; neither => skip.

REPLACES THE MASS-DELETION CIRCUIT BREAKER (maintainer directive):
golden_repo_reconciler's ABSENCE question fails unsafely under a stale
NFS mount (os.path.exists() False for everything at once), hence its
ratio breaker. This module asks a SUPERSESSION question from positive,
durable evidence (pointer files) instead -- a stale mount can only make a
pointer unreadable (a per-repo, fail-closed skip), never make a snapshot
falsely appear superseded. NO ratio threshold/confirmation
counter/abort-on-too-many heuristic anywhere here: Bug #1382 is the
precedent for why that backfires (a genuine orphan set tripped a >50%
breaker every restart for ~2 months, healing nothing). The honest
substitute is `mode`: "report" (default, fail-closed) computes and logs
candidates WITHOUT scheduling deletion; "delete" is an explicit operator
promotion.

FACTS RECORDED, NOT ACTED ON: `previous_path` is NOT a rollback mechanism
(no rollback code exists; `_execute_refresh` schedules it for deletion
BEFORE `_enforce_retention` protects it -- real rollback depth is
`snapshot_retention_keep_last`). `QueryTracker` refcount (downstream, in
CleanupManager) is PROCESS-LOCAL -- the minimum-retention-age floor is the
only bounded cross-process guard. Deleting a superseded snapshot can break
`git fetch golden` for an activation created against it until its next
sync -- expected, not corruption.

Both of CleanupManager's gates (refcount-zero, min-retention-age) are
unchanged; this module only decides WHICH paths reach them (or are merely
reported). Cluster single-flight via register_job_if_no_conflict/
DuplicateJobError.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Set, Tuple

# Bug #1567 Gap 1: the pointer-reading + ts_live-anchored supersession
# predicate is now defined ONCE in global_repos/snapshot_retention.py and
# imported here -- this module previously carried its OWN copy, and two
# independent implementations of "what is superseded" would drift (drift
# is a deletion). Re-exported under the SAME names (via __all__ below) for
# backward compatibility with existing test imports
# (`from ...versioned_snapshot_reconciler import
# compute_snapshot_deletion_candidates, read_pointer_target_and_previous`).
from code_indexer.global_repos.snapshot_retention import (
    DEFAULT_MIN_ABSOLUTE_AGE_SECONDS,
    _protected_snapshot_paths,
    collect_all_pointers,
    compute_snapshot_deletion_candidates,
    globally_referenced_paths,
    parse_live_timestamp,
    read_pointer_target_and_previous,
    resolve_governing_pointer,
    resolve_retention_keep_last,
)
from code_indexer.server.services.job_tracker import DuplicateJobError

__all__ = [
    "_protected_snapshot_paths",
    "collect_all_pointers",
    "compute_snapshot_deletion_candidates",
    "globally_referenced_paths",
    "parse_live_timestamp",
    "read_pointer_target_and_previous",
    "resolve_governing_pointer",
    "reconcile_versioned_snapshots",
    "VersionedSnapshotReconcileResult",
]

if TYPE_CHECKING:
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

logger = logging.getLogger(__name__)

DEFAULT_RECONCILE_SUBMITTER = "system-versioned-snapshot-reconcile"
RECONCILE_OPERATION_TYPE = "versioned_snapshot_reconcile_sweep"
RECONCILE_SWEEP_SENTINEL_ALIAS = "__versioned_snapshot_reconcile_sweep__"

SweepMode = Literal["report", "delete"]
_VALID_MODES = ("report", "delete")

#: job_tracker is typed Any deliberately: callers pass either the real
#: JobTracker (server/services/job_tracker.py) or a duck-typed test double
#: exposing register_job_if_no_conflict/complete_job/fail_job. A concrete
#: type here would force importing JobTracker's full dependency chain into
#: every caller of this module, including solo/CLI contexts that never
#: construct one.
JobTrackerLike = Any


@dataclass
class VersionedSnapshotReconcileResult:
    """Summary of one orphan-sweep pass over `.versioned/`."""

    mode: str = "report"
    scanned_namespaces: List[str] = field(default_factory=list)
    #: bare namespace -> reason skipped. Never set for a reconciled one.
    skipped_namespaces: Dict[str, str] = field(default_factory=dict)
    #: Candidates found safe to delete. In "report" mode NEVER scheduled
    #: (recorded for visibility only). In "delete" mode every entry was
    #: also passed to cleanup_manager.schedule_cleanup().
    scheduled_paths: List[str] = field(default_factory=list)
    #: True only when the WHOLE sweep was refused (base-dir OSError or a
    #: single-flight conflict) -- never for a per-repo skip.
    aborted: bool = False
    abort_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Orphan sweep orchestration
# ---------------------------------------------------------------------------


def _acquire_single_flight_or_abort(
    job_tracker: Optional[JobTrackerLike],
    submitter_username: str,
    result: VersionedSnapshotReconcileResult,
) -> Optional[str]:
    """None if no job_tracker wired. Otherwise claims the cluster-atomic
    single-flight lock; on ANY failure (conflict or error), marks
    *result* aborted and returns None."""
    if job_tracker is None:
        return None
    job_id = uuid.uuid4().hex
    try:
        job_tracker.register_job_if_no_conflict(
            job_id=job_id,
            operation_type=RECONCILE_OPERATION_TYPE,
            username=submitter_username,
            repo_alias=RECONCILE_SWEEP_SENTINEL_ALIAS,
        )
        return job_id
    except DuplicateJobError:
        result.aborted = True
        result.abort_reason = (
            "another worker/node is already running the versioned-"
            "snapshot orphan sweep -- skipping (single-flight guard)."
        )
    except Exception as claim_error:  # noqa: BLE001 -- reconcile safety
        result.aborted = True
        result.abort_reason = (
            f"failed to claim the single-flight sweep lock (refusing to "
            f"proceed without it): {claim_error}"
        )
    logger.info("Bug #1567 reconcile: %s", result.abort_reason)
    return None


def _finalize_sweep_job(
    job_tracker: Optional[JobTrackerLike],
    job_id: Optional[str],
    result: VersionedSnapshotReconcileResult,
    sweep_error: Optional[Exception],
) -> None:
    """Report success/failure of the sweep to job_tracker, if wired."""
    if job_tracker is None or job_id is None:
        return
    try:
        if sweep_error is not None:
            job_tracker.fail_job(job_id, error=str(sweep_error))
        else:
            job_tracker.complete_job(
                job_id,
                result={
                    "mode": result.mode,
                    "scanned": len(result.scanned_namespaces),
                    "candidates": len(result.scheduled_paths),
                    "skipped": len(result.skipped_namespaces),
                },
            )
    except Exception as bookkeeping_error:  # noqa: BLE001
        logger.error(
            "Bug #1567 reconcile: job-tracker bookkeeping failed: %s",
            bookkeeping_error,
        )


def reconcile_versioned_snapshots(
    golden_repos_dir: str,
    *,
    snapshot_manager: Optional["VersionedSnapshotManager"],
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
    job_tracker: Optional[JobTrackerLike] = None,
    retention_keep_last: Optional[int] = None,
    min_absolute_age_seconds: float = DEFAULT_MIN_ABSOLUTE_AGE_SECONDS,
    mode: SweepMode = "report",
    submitter_username: str = DEFAULT_RECONCILE_SUBMITTER,
) -> VersionedSnapshotReconcileResult:
    """Scan `.versioned/` for superseded snapshots and, in "delete" mode,
    schedule their deletion through the existing refcount+retention-age-
    gated CleanupManager. See the module docstring for the full contract.

    ``alias_manager`` is used ONLY for its ``aliases_dir`` path -- pointer
    files are read directly (module docstring hole A). ``mode="report"``
    (default) is fail-closed: candidates are computed and recorded but
    never scheduled; ``"delete"`` is an explicit operator promotion.

    Raises:
        ValueError: *mode* is neither "report" nor "delete".
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")

    result = VersionedSnapshotReconcileResult(mode=mode)
    if snapshot_manager is None:
        return result

    job_id = _acquire_single_flight_or_abort(job_tracker, submitter_username, result)
    if result.aborted:
        return result

    sweep_error: Optional[Exception] = None
    try:
        _run_sweep(
            golden_repos_dir,
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
            retention_keep_last=retention_keep_last,
            min_absolute_age_seconds=min_absolute_age_seconds,
            mode=mode,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 -- reconcile safety
        logger.error("Bug #1567 reconcile: sweep failed unexpectedly: %s", exc)
        result.aborted = True
        result.abort_reason = f"reconcile sweep failed unexpectedly: {exc}"
        sweep_error = exc

    _finalize_sweep_job(job_tracker, job_id, result, sweep_error)
    return result


def _enumerate_versioned_namespaces(versioned_dir: Path) -> Optional[List[str]]:
    """Sorted immediate subdirectory names. None (not []) on
    FileNotFoundError -- "not provisioned yet" is normal and must not be
    confused with a genuine OSError health-gate failure (NOT swallowed)."""
    try:
        return sorted(entry.name for entry in versioned_dir.iterdir() if entry.is_dir())
    except FileNotFoundError:
        return None


@dataclass
class _SweepContext:
    namespace_entries: List[str]
    pointers: Dict[str, Tuple[str, Optional[str]]]
    referenced_paths: Set[str]
    keep_last: int


def _build_sweep_context(
    golden_repos_path: Path,
    alias_manager: "AliasManager",
    retention_keep_last: Optional[int],
    result: VersionedSnapshotReconcileResult,
) -> Optional[_SweepContext]:
    """Resolves both base-directory health gates plus the global pointer
    union, ONCE per sweep. None (with *result* marked aborted) if either
    base directory is unhealthy."""
    versioned_dir = golden_repos_path / ".versioned"
    try:
        namespace_entries = _enumerate_versioned_namespaces(versioned_dir)
    except OSError as scan_error:
        result.aborted = True
        result.abort_reason = (
            f"'{versioned_dir}' is not a healthy/readable directory -- "
            f"refusing to sweep (likely an infra/mount problem): {scan_error}"
        )
        logger.warning("Bug #1567 reconcile: %s", result.abort_reason)
        return None
    if namespace_entries is None:
        return _SweepContext([], {}, set(), 1)  # nothing provisioned -- empty sweep

    try:
        pointers = collect_all_pointers(alias_manager.aliases_dir)
    except OSError as aliases_error:
        result.aborted = True
        result.abort_reason = (
            f"'{alias_manager.aliases_dir}' is not a healthy/readable "
            f"directory -- refusing to sweep (likely an infra/mount "
            f"problem): {aliases_error}"
        )
        logger.warning("Bug #1567 reconcile: %s", result.abort_reason)
        return None

    keep_last = (
        retention_keep_last
        if retention_keep_last is not None
        else resolve_retention_keep_last()
    )
    return _SweepContext(
        namespace_entries, pointers, globally_referenced_paths(pointers), keep_last
    )


def _run_sweep(
    golden_repos_dir: str,
    *,
    snapshot_manager: "VersionedSnapshotManager",
    alias_manager: "AliasManager",
    cleanup_manager: "CleanupManager",
    retention_keep_last: Optional[int],
    min_absolute_age_seconds: float,
    mode: SweepMode,
    result: VersionedSnapshotReconcileResult,
) -> None:
    """Enumerate `.versioned/`, build the pointer union ONCE, reconcile
    every namespace against that SAME snapshot. Only the two
    base-directory health gates may abort the whole sweep."""
    golden_repos_path = Path(golden_repos_dir)
    context = _build_sweep_context(
        golden_repos_path, alias_manager, retention_keep_last, result
    )
    if context is None:
        return

    for bare_namespace in context.namespace_entries:
        result.scanned_namespaces.append(bare_namespace)
        _reconcile_one_namespace(
            bare_namespace,
            golden_repos_path=golden_repos_path,
            snapshot_manager=snapshot_manager,
            cleanup_manager=cleanup_manager,
            pointers=context.pointers,
            referenced_paths=context.referenced_paths,
            keep_last=context.keep_last,
            min_absolute_age_seconds=min_absolute_age_seconds,
            mode=mode,
            result=result,
        )


def _reconcile_one_namespace(
    bare_namespace: str,
    *,
    golden_repos_path: Path,
    snapshot_manager: "VersionedSnapshotManager",
    cleanup_manager: "CleanupManager",
    pointers: Dict[str, Tuple[str, Optional[str]]],
    referenced_paths: Set[str],
    keep_last: int,
    min_absolute_age_seconds: float,
    mode: SweepMode,
    result: VersionedSnapshotReconcileResult,
) -> None:
    """Reconcile ONE namespace. Genuinely never raises -- any failure
    records ``result.skipped_namespaces[bare_namespace]`` (fail-closed,
    zero deletions) and returns, letting the sweep continue."""
    try:
        _reconcile_one_namespace_body(
            bare_namespace,
            golden_repos_path=golden_repos_path,
            snapshot_manager=snapshot_manager,
            cleanup_manager=cleanup_manager,
            pointers=pointers,
            referenced_paths=referenced_paths,
            keep_last=keep_last,
            min_absolute_age_seconds=min_absolute_age_seconds,
            mode=mode,
            result=result,
        )
    except Exception as namespace_error:  # noqa: BLE001 -- per-repo isolation
        result.skipped_namespaces[bare_namespace] = (
            f"reconciliation failed (non-fatal, skipping this repo, zero "
            f"deletions): {namespace_error}"
        )
        logger.warning(
            "Bug #1567 reconcile: namespace '%s' failed to reconcile "
            "(non-fatal, skipping this repo entirely -- zero deletions): "
            "%s: %s",
            bare_namespace,
            type(namespace_error).__name__,
            namespace_error,
        )


def _debug_log_namespace_decision(
    bare_namespace: str,
    *,
    ts_live: int,
    snapshots: List[Tuple[str, int]],
    referenced_paths: Set[str],
    keep_last: int,
    candidates: List[str],
) -> None:
    """Bug #1567c: DEBUG-only breakdown of WHY each snapshot in this
    namespace was kept or became a candidate. Purely observational --
    computed from the same inputs compute_snapshot_deletion_candidates
    already used, but never influences which paths get deleted. An
    operator promoting versioned_snapshot_reconcile_config.mode to
    "delete" needs to see this reasoning first.

    The "kept" buckets below can overlap (a snapshot can be both
    referenced by a pointer AND within keep-last-N) -- this is a
    diagnostic breakdown, not a strict partition.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    older = sorted(
        ((p, ts) for p, ts in snapshots if ts < ts_live), key=lambda item: item[1]
    )
    at_or_newer_than_live = sum(1 for _p, ts in snapshots if ts >= ts_live)
    all_paths_here = {p for p, _ts in snapshots}
    # Mirrors _protected_snapshot_paths's own referenced-path bucket
    # exactly (referenced_paths & all_paths_here) -- NOT restricted to
    # older-only paths, since a live/newer snapshot can also be
    # separately referenced by another pointer.
    referenced_kept = referenced_paths & all_paths_here
    keep_from_history = max(keep_last - 1, 0)
    keep_last_kept = (
        {p for p, _ts in older[-keep_from_history:]} if keep_from_history else set()
    )
    logger.debug(
        "Bug #1567 reconcile: namespace '%s' decision -- ts_live=%d "
        "total_snapshots=%d at_or_newer_than_live(kept)=%d older=%d "
        "referenced_by_pointer(kept)=%d within_keep_last_%d(kept)=%d "
        "candidates=%d",
        bare_namespace,
        ts_live,
        len(snapshots),
        at_or_newer_than_live,
        len(older),
        len(referenced_kept),
        keep_last,
        len(keep_last_kept),
        len(candidates),
    )


def _reconcile_one_namespace_body(
    bare_namespace: str,
    *,
    golden_repos_path: Path,
    snapshot_manager: "VersionedSnapshotManager",
    cleanup_manager: "CleanupManager",
    pointers: Dict[str, Tuple[str, Optional[str]]],
    referenced_paths: Set[str],
    keep_last: int,
    min_absolute_age_seconds: float,
    mode: SweepMode,
    result: VersionedSnapshotReconcileResult,
) -> None:
    governing = resolve_governing_pointer(bare_namespace, pointers)
    if governing is None:
        result.skipped_namespaces[bare_namespace] = (
            "alias pointer missing or unreadable -- skipping entirely "
            "(fail-closed, zero deletions for this repo)"
        )
        logger.warning(
            "Bug #1567 reconcile: namespace '%s' has no readable alias "
            "pointer -- skipping entirely (zero deletions for this repo).",
            bare_namespace,
        )
        return

    target_path, _previous_path = governing
    # Bug #1567 Gap 1: parse_live_timestamp is now a pure structural check
    # with no namespace-string argument (see snapshot_retention.py's
    # path-rooting + sanitization-mismatch notes); golden_repos_path
    # remains this function's own parameter, used by
    # compute_snapshot_deletion_candidates's signature-compat kwarg below.
    # Bug #1567c: capture the parsed timestamp instead of discarding it --
    # reused below by _debug_log_namespace_decision's operator-facing
    # decision breakdown.
    ts_live = parse_live_timestamp(target_path)
    if ts_live is None:
        # target_path resolved but is NOT an interpretable v_<ts>
        # snapshot under THIS namespace (e.g. the master clone on first
        # refresh, a write-mode source, or a foreign path). Record it as
        # skipped rather than silently falling through to "zero
        # candidates, nothing to see here" -- an operator needs to be
        # able to distinguish "genuinely nothing superseded" from
        # "this repo's pointer is anomalous".
        result.skipped_namespaces[bare_namespace] = (
            "governing pointer's target_path is not an interpretable "
            "versioned snapshot for this namespace -- skipping entirely "
            "(fail-closed, zero deletions for this repo)"
        )
        logger.warning(
            "Bug #1567 reconcile: namespace '%s' pointer target_path "
            "'%s' is not a v_<ts> snapshot under this namespace -- "
            "skipping entirely (zero deletions for this repo).",
            bare_namespace,
            target_path,
        )
        return

    snapshots = snapshot_manager.list_snapshots(bare_namespace)
    if not snapshots:
        return

    candidates = compute_snapshot_deletion_candidates(
        bare_namespace,
        golden_repos_dir=golden_repos_path,
        snapshots=snapshots,
        target_path=target_path,
        referenced_paths=referenced_paths,
        keep_last=keep_last,
        min_absolute_age_seconds=min_absolute_age_seconds,
    )

    _debug_log_namespace_decision(
        bare_namespace,
        ts_live=ts_live,
        snapshots=snapshots,
        referenced_paths=referenced_paths,
        keep_last=keep_last,
        candidates=candidates,
    )

    for path in candidates:
        # Structural re-confirmation immediately before enqueue: must be
        # a genuine versioned snapshot, never the master clone.
        if not snapshot_manager.is_versioned_snapshot(path):
            continue
        result.scheduled_paths.append(path)
        if mode == "delete":
            cleanup_manager.schedule_cleanup(path)
