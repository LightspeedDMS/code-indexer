"""
Golden Repo Registry Reconciler (Bug #1317).

Detects `golden_repos` registry rows -- PostgreSQL in cluster mode, SQLite
in solo mode -- whose on-disk clone is absent ("registry-orphans") and
removes them via the existing remove_golden_repo() cascade so the row, the
alias pointer file, and the global registry entry are all cleaned up
consistently. Without this, steady-state search keeps reporting confusing
per-repo "not found on filesystem" errors for rows that can never resolve.

This module deliberately does NOT re-implement removal/cascade logic
(Messi Rule #4, anti-duplication): remove_golden_repo() already handles a
missing on-disk clone gracefully (treats GoldenRepoNotFoundError as "nothing
to clean up on disk" and still removes the registry row, the alias pointer,
the global registry entry, and cascades to any activated repos), so
reconcile just needs to find the orphans and call it.

SAFETY (code-review hardening, Bug #1317 follow-up):

`get_actual_repo_path()` decides "absent" via `os.path.exists()`, which
swallows EVERY OSError (ENOENT, ESTALE, EIO, ETIMEDOUT, EACCES) and returns
False. This project has a documented reality
(project_nfs_host_down_hangs_systemd.md): when the CoW/NFS host node is
down, hard NFS mounts on other nodes go stale/hang, so `exists()` returns
False for EVERY repo -- indistinguishable from "all orphaned" at this
layer. A destructive sweep must never let mount health decide whether it
tears down the whole registry. Two guards defend against this:

1. A positive health check on `golden_repos_dir` itself before sweeping
   (`_golden_repos_dir_is_healthy`) -- skips the sweep entirely if the base
   directory is not a healthy, accessible directory.
2. A circuit-breaker (`ORPHAN_FRACTION_ABORT_THRESHOLD`): a real orphan set
   is always a small minority of the registry. If more than half of all
   registered repos resolve absent, the sweep aborts without deleting
   anything -- that shape means an infra/mount problem, not orphans.

Additionally, a healthy repo (clone resolves) that is registered as
globally active but whose `-global` alias pointer file is missing (the
#1315 fallback symptom) is REPAIRED by re-writing the pointer -- never
deleted. This is the real steady-state cure for that symptom; #1315's
index_path fallback remains the safety net for anything reconcile does not
(yet) observe.

Finally, a single-flight guard (via the existing
`job_tracker.register_job_if_no_conflict` / `DuplicateJobError` cluster-
atomic gate, the same primitive already used by
`GoldenRepoManager._register_lifecycle_after_registration`) prevents every
uvicorn worker / cluster node from firing a duplicate sweep at startup.
When no `job_tracker` is wired (e.g. tests, solo CLI paths), the guard is
simply skipped -- the sweep itself is safe to run concurrently since
`remove_golden_repo()` already tolerates a second call for an alias another
worker already removed (raises, caught here as `orphans_failed`).
"""

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from code_indexer.server.services.job_tracker import DuplicateJobError

logger = logging.getLogger(__name__)

DEFAULT_RECONCILE_SUBMITTER = "system-reconcile"

# A real orphan set is always a small minority of the registry. Anything
# above this fraction is treated as an infra/mount problem, not orphans.
ORPHAN_FRACTION_ABORT_THRESHOLD = 0.5

RECONCILE_OPERATION_TYPE = "golden_repo_reconcile_sweep"
RECONCILE_SWEEP_SENTINEL_ALIAS = "__golden_repo_reconcile_sweep__"


@dataclass
class ReconcileResult:
    """Summary of one reconcile pass over the golden_repos registry."""

    orphans_found: List[str] = field(default_factory=list)
    orphans_removed: List[str] = field(default_factory=list)
    orphans_failed: List[str] = field(default_factory=list)
    pointers_repaired: List[str] = field(default_factory=list)
    pointers_repair_failed: List[str] = field(default_factory=list)
    healthy_count: int = 0
    aborted: bool = False
    abort_reason: Optional[str] = None


def _golden_repos_dir_is_healthy(golden_repos_dir: str) -> bool:
    """
    Positive health confirmation for the base golden-repos directory.

    Returns False (never raises) on ANY OSError -- including the stale/hung
    NFS symptoms documented in project_nfs_host_down_hangs_systemd.md -- so
    a sweep never proceeds against an unhealthy mount.
    """
    try:
        return os.path.isdir(golden_repos_dir)
    except OSError as health_error:
        logger.warning(
            "Bug #1317 reconcile: golden_repos_dir '%s' health check failed "
            "(%s) -- treating as unhealthy, skipping sweep.",
            golden_repos_dir,
            health_error,
        )
        return False


def _claim_sweep_single_flight(job_tracker: Any, submitter_username: str) -> str:
    """
    Claim the cluster-atomic single-flight lock for this sweep.

    Callers must only invoke this when a job_tracker is actually wired (see
    reconcile_golden_repo_registry) -- there is nothing to claim otherwise.

    Returns the claimed job_id.

    Raises DuplicateJobError if another worker/node already claimed it.
    """
    job_id = uuid.uuid4().hex
    job_tracker.register_job_if_no_conflict(
        job_id=job_id,
        operation_type=RECONCILE_OPERATION_TYPE,
        username=submitter_username,
        repo_alias=RECONCILE_SWEEP_SENTINEL_ALIAS,
    )
    return job_id


def reconcile_golden_repo_registry(
    golden_repo_manager: GoldenRepoManager,
    submitter_username: str = DEFAULT_RECONCILE_SUBMITTER,
) -> ReconcileResult:
    """
    Scan the golden_repos registry for rows with no on-disk clone and
    submit their removal; repair healthy global repos with a missing alias
    pointer. See module docstring for the full safety contract.

    Args:
        golden_repo_manager: The shared GoldenRepoManager instance.
        submitter_username: Username recorded on the submitted removal jobs
            and the single-flight coordination job.

    Returns:
        ReconcileResult summarizing orphans found/removed/failed, pointer
        repairs, the healthy count, and whether the sweep aborted early.
    """
    result = ReconcileResult()

    job_tracker = getattr(golden_repo_manager, "job_tracker", None)
    sweep_job_id: Optional[str] = None
    if job_tracker is not None:
        try:
            sweep_job_id = _claim_sweep_single_flight(job_tracker, submitter_username)
        except DuplicateJobError:
            result.aborted = True
            result.abort_reason = (
                "another worker/node is already running the golden-repo "
                "registry reconcile sweep -- skipping (single-flight guard)."
            )
            logger.info("Bug #1317 reconcile: %s", result.abort_reason)
            return result

    try:
        _run_sweep(golden_repo_manager, submitter_username, result)
    except Exception as sweep_error:  # noqa: BLE001 -- sidecar discipline
        logger.error("Bug #1317 reconcile: sweep failed unexpectedly: %s", sweep_error)
        result.aborted = True
        result.abort_reason = f"reconcile sweep failed unexpectedly: {sweep_error}"
        if job_tracker is not None and sweep_job_id is not None:
            try:
                job_tracker.fail_job(sweep_job_id, error=str(sweep_error))
            except Exception:  # noqa: BLE001
                pass
        return result

    if job_tracker is not None and sweep_job_id is not None:
        try:
            job_tracker.complete_job(
                sweep_job_id,
                result={
                    "orphans_removed": len(result.orphans_removed),
                    "pointers_repaired": len(result.pointers_repaired),
                },
            )
        except Exception as complete_error:  # noqa: BLE001
            logger.error(
                "Bug #1317 reconcile: failed to mark sweep job complete: %s",
                complete_error,
            )

    return result


def _run_sweep(
    golden_repo_manager: GoldenRepoManager,
    submitter_username: str,
    result: ReconcileResult,
) -> None:
    """Core sweep logic: health check -> classify -> circuit-breaker ->
    remove orphans -> repair missing pointers on healthy repos."""
    if not _golden_repos_dir_is_healthy(golden_repo_manager.golden_repos_dir):
        result.aborted = True
        result.abort_reason = (
            f"golden_repos_dir '{golden_repo_manager.golden_repos_dir}' is "
            "not a healthy/mounted directory -- refusing to reconcile "
            "(likely an infra/mount problem, not orphans)."
        )
        logger.warning("Bug #1317 reconcile: %s", result.abort_reason)
        return

    repo_rows = golden_repo_manager.list_golden_repos()

    # Pass 1: classify every registered repo WITHOUT deleting anything yet.
    orphan_candidates: List[str] = []
    healthy_rows: List[Any] = []
    for repo_row in repo_rows:
        alias = repo_row["alias"]
        try:
            actual_path = golden_repo_manager.get_actual_repo_path(alias)
            result.healthy_count += 1
            healthy_rows.append((repo_row, actual_path))
        except GoldenRepoNotFoundError:
            orphan_candidates.append(alias)

    total = len(repo_rows)
    if total > 0:
        absent_fraction = len(orphan_candidates) / total
        if absent_fraction > ORPHAN_FRACTION_ABORT_THRESHOLD:
            result.orphans_found = orphan_candidates
            result.aborted = True
            result.abort_reason = (
                f"refusing to reconcile: {len(orphan_candidates)}/{total} "
                "golden repos resolve absent -- likely an infra/mount "
                "problem, not orphans."
            )
            logger.warning("Bug #1317 reconcile: %s", result.abort_reason)
            return

    # Pass 2: safe to proceed -- remove confirmed orphans.
    for alias in orphan_candidates:
        result.orphans_found.append(alias)
        logger.warning(
            "Bug #1317 reconcile: golden repo '%s' is a registry-orphan "
            "(no on-disk clone) -- submitting removal.",
            alias,
        )
        try:
            golden_repo_manager.remove_golden_repo(
                alias, submitter_username=submitter_username
            )
            result.orphans_removed.append(alias)
        except Exception as removal_error:  # noqa: BLE001 -- reconcile safety
            logger.error(
                "Bug #1317 reconcile: failed to submit removal for "
                "registry-orphan '%s': %s",
                alias,
                removal_error,
            )
            result.orphans_failed.append(alias)

    # Pass 3: repair healthy global repos with a missing alias pointer (the
    # #1315 fallback symptom) -- clone/index intact, registry says global,
    # only the pointer JSON is gone. Never delete a healthy repo for this;
    # just re-write the pointer (idempotent).
    from code_indexer.global_repos.alias_manager import AliasManager

    aliases_dir = os.path.join(golden_repo_manager.golden_repos_dir, "aliases")
    alias_manager = AliasManager(aliases_dir)

    for repo_row, actual_path in healthy_rows:
        alias = repo_row["alias"]
        global_alias = f"{alias}-global"
        try:
            if not golden_repo_manager.is_globally_active(alias):
                continue
            if alias_manager.alias_exists(global_alias):
                continue
            alias_manager.create_alias(
                alias_name=global_alias, target_path=actual_path, repo_name=alias
            )
            result.pointers_repaired.append(alias)
            logger.warning(
                "Bug #1317 reconcile: repaired missing alias pointer for "
                "globally-active repo '%s' (clone healthy, pointer was "
                "missing -- #1315 fallback symptom).",
                alias,
            )
        except Exception as repair_error:  # noqa: BLE001
            logger.error(
                "Bug #1317 reconcile: failed to repair alias pointer for '%s': %s",
                alias,
                repair_error,
            )
            result.pointers_repair_failed.append(alias)
