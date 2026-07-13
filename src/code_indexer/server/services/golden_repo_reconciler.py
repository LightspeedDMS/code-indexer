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
   anything on the first occurrence -- that shape usually means an
   infra/mount problem, not orphans (see Bug #1382 below for the
   confirmation-based exception).

CIRCUIT-BREAKER CONFIRMATION (Bug #1382 follow-up):

A live staging incident showed the Bug #1317 circuit-breaker above has no
path to recovery: a genuine, persistent registry-orphan set (8/14 = 57%,
from a crash-recovery gap where the DB was restored but on-disk clones were
not) tripped the >50% threshold on EVERY restart for ~2 months, with only a
repeated log-only WARNING and no way to ever clean up. Two additions close
this gap without weakening the original protection:

1. Persisted, cross-restart confirmation counter
   (`golden_repo_reconcile_breaker_state` table -- SQLite solo /
   PostgreSQL cluster, via the SAME `_sqlite_backend` GoldenRepoManager
   already uses, Messi Rule #4 anti-duplication). Each high-ratio abort
   records a stable fingerprint (sorted, comma-joined) of the orphan-
   candidate alias set. If the SAME fingerprint is observed on
   `CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD` (3) CONSECUTIVE sweeps, each
   with a healthy `golden_repos_dir`, the sweep treats this as confirmed
   genuine orphans (not an infra blip) and proceeds with removal. A
   base-dir-unhealthy event (real infra flapping) or a normal within-
   threshold sweep resets the counter, so genuine NFS instability -- or an
   unstable/changing absent-repo set -- can never accumulate toward
   confirmation. This preserves the original safety: a one-off high-ratio
   event still aborts every time on first occurrence.
2. Health-check escalation (`HealthCheckService.
   _collect_golden_repo_reconcile_breaker_failures`): a currently-tripped
   breaker now surfaces as a DEGRADED `/health` `failure_reasons` entry
   immediately (not after months of silent restarts), reusing the existing
   admin-visible health surface rather than inventing a new alerting
   mechanism.

WALL-CLOCK OBSERVATION GATING (Bug #1382 follow-up, rolling-deploy hardening):

The confirmation counter above counts SWEEPS, not elapsed time. This
project's cluster does rolling deploys: node-1, node-2, and node-3 each
restart (and each run this sweep once at startup) within a few minutes of
each other. Without gating, that single rolling-deploy event alone reaches
3 "consecutive" confirmations -- collapsing what was designed to be
evidence "genuinely spread over real operational time" into "one restart
wave," a much weaker safety bar than intended. `_record_breaker_observation_with_time_gate`
closes this: a same-fingerprint observation is only persisted as a new
confirmation (and only increments the count) if at least
`MIN_BREAKER_OBSERVATION_GAP_SECONDS` has elapsed since the previously
recorded `last_observed_at`; an observation arriving sooner is treated as a
duplicate no-op (count and persisted state both left unchanged). A
DIFFERENT fingerprint, or the base-dir-unhealthy reset, are never gated --
only the same-fingerprint increment path is.

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

# Bug #1382: number of CONSECUTIVE sweeps that must observe the SAME
# orphan-candidate set, each with a healthy golden_repos_dir, before the
# circuit-breaker treats a high absent-fraction as confirmed genuine
# orphans (rather than an infra/mount blip) and proceeds with removal
# despite exceeding ORPHAN_FRACTION_ABORT_THRESHOLD. See
# golden_repo_reconcile_breaker_state (sqlite_backends.py /
# golden_repo_metadata_backend.py) for the persisted cross-restart counter.
CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD = 3

# Bug #1382 rolling-deploy hardening: minimum wall-clock gap (seconds) that
# must elapse since the previously recorded observation before a
# same-fingerprint sweep is allowed to increment the consecutive-
# confirmation count. This project's cluster does rolling deploys where
# node-1/node-2/node-3 each restart (and each run this sweep once at
# startup) within minutes of each other; without this gate, one rolling
# deploy alone could reach CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD "consecutive"
# confirmations, collapsing what is supposed to be evidence "genuinely
# spread over real operational time" into "one restart wave." 30 minutes
# comfortably exceeds a typical multi-node rolling-restart window (minutes)
# while being negligible against the real cadence of genuinely separate
# restarts (hours/days) -- the actual historical incident this all traces
# back to (Bug #1382's staging incident) persisted for ~2 months.
MIN_BREAKER_OBSERVATION_GAP_SECONDS = 30 * 60

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
    # Bug #1382: circuit-breaker confirmation bookkeeping. Only meaningful
    # when the absent-fraction exceeded ORPHAN_FRACTION_ABORT_THRESHOLD this
    # sweep; 0 otherwise.
    circuit_breaker_consecutive_count: int = 0
    circuit_breaker_confirmed_proceed: bool = False


def _get_breaker_backend(golden_repo_manager: GoldenRepoManager) -> Optional[Any]:
    """
    Resolve the shared golden-repo-metadata storage backend used to persist
    circuit-breaker confirmation state across restarts (Bug #1382).

    This is the SAME backend instance GoldenRepoManager already uses for its
    own registry rows (`_sqlite_backend` -- SQLite in solo mode, PostgreSQL
    in cluster mode via StorageFactory), reused here rather than inventing a
    new storage layer (Messi Rule #4: anti-duplication). Returns None if
    unavailable; callers degrade gracefully to "always treat as
    unconfirmed".
    """
    return getattr(golden_repo_manager, "_sqlite_backend", None)


def _record_breaker_observation(backend: Optional[Any], fingerprint: str) -> int:
    """
    Persist one high-ratio observation and return the resulting consecutive
    confirmation count (Bug #1382).

    Fails safe to 1 (i.e. "not yet confirmed") on any backend error or
    unavailability -- a bookkeeping failure must never silently grant
    confirmation to proceed with deletion.
    """
    if backend is None or not hasattr(backend, "record_reconcile_breaker_observation"):
        return 1
    try:
        return int(backend.record_reconcile_breaker_observation(fingerprint))
    except Exception as breaker_error:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Bug #1382 reconcile: failed to persist circuit-breaker "
            "observation: %s -- treating as unconfirmed (safe default).",
            breaker_error,
        )
        return 1


def _reset_breaker_state(backend: Optional[Any]) -> None:
    """
    Clear any persisted circuit-breaker confirmation state (Bug #1382).

    Called whenever a sweep sees evidence AGAINST the "stable persistent
    orphan set" hypothesis: a normal low-ratio sweep, or a base-directory
    health-check failure (real infra flapping voids prior confirmations --
    see module docstring).
    """
    if backend is None or not hasattr(backend, "reset_reconcile_breaker_state"):
        return
    try:
        backend.reset_reconcile_breaker_state()
    except Exception as reset_error:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Bug #1382 reconcile: failed to reset circuit-breaker state: %s",
            reset_error,
        )


def _parse_observed_at(value: Any) -> Optional[datetime]:
    """
    Normalize a persisted `last_observed_at` value into a tz-aware UTC
    datetime (Bug #1382 rolling-deploy hardening).

    The two backends store this differently: SQLite persists an
    ISO-8601 string (`datetime.now(timezone.utc).isoformat()`), while
    PostgreSQL persists a native `datetime` object. Both shapes must be
    handled here since the gating logic is backend-agnostic. Returns None
    on `None` input or an unparseable string -- callers treat that as "gap
    unknown" and must NOT gate on it (a parse failure must never silently
    block confirmation forever).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return (
            parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        )
    return None


def _peek_breaker_state(backend: Optional[Any]) -> Optional[Dict[str, Any]]:
    """
    Read the currently persisted circuit-breaker state without recording a
    new observation (Bug #1382 rolling-deploy hardening).

    Fails safe to None (i.e. "no prior state") on any backend error or
    unavailability -- the caller falls through to recording a normal
    observation rather than silently granting/denying confirmation based on
    a bookkeeping failure.
    """
    if backend is None or not hasattr(backend, "get_reconcile_breaker_state"):
        return None
    try:
        return backend.get_reconcile_breaker_state()  # type: ignore[no-any-return]
    except Exception as peek_error:  # noqa: BLE001 -- best-effort bookkeeping
        logger.error(
            "Bug #1382 reconcile: failed to peek circuit-breaker state: %s "
            "-- treating as no prior state (safe default).",
            peek_error,
        )
        return None


def _record_breaker_observation_with_time_gate(
    backend: Optional[Any], fingerprint: str
) -> int:
    """
    Record a high-ratio observation, but only increment the persisted
    consecutive-confirmation count for a same-fingerprint observation when
    at least MIN_BREAKER_OBSERVATION_GAP_SECONDS has elapsed since the
    previously recorded observation (Bug #1382 rolling-deploy hardening).

    This closes a gap in the original Bug #1382 confirmation counter: a
    multi-node rolling deploy runs this sweep once per node, all within
    minutes of each other at startup, which could otherwise reach
    CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD "consecutive" confirmations from
    a single rolling-deploy event rather than genuinely time-separated
    incidents.

    A DIFFERENT fingerprint (or no prior state at all) is NEVER gated --
    the reset-to-1 / first-ever-observation behavior is unchanged. The gate
    applies ONLY to the same-fingerprint increment path, and only when the
    elapsed time since the prior observation is known and below the
    threshold; an unparseable/missing `last_observed_at` is treated as "gap
    unknown" and falls through to normal recording (never gates forever on
    a parse failure).
    """
    prior_state = _peek_breaker_state(backend)
    if prior_state is not None and prior_state.get("orphan_fingerprint") == fingerprint:
        prior_observed_at = _parse_observed_at(prior_state.get("last_observed_at"))
        if prior_observed_at is not None:
            elapsed_seconds = (
                datetime.now(timezone.utc) - prior_observed_at
            ).total_seconds()
            if elapsed_seconds < MIN_BREAKER_OBSERVATION_GAP_SECONDS:
                unchanged_count = int(prior_state.get("consecutive_count", 1))
                logger.info(
                    "Bug #1382 reconcile: same-fingerprint observation "
                    "arrived only %.0fs after the previous one (< %ds "
                    "gate) -- treating as a duplicate/no-op (likely a "
                    "multi-node rolling deploy), count stays at %d.",
                    elapsed_seconds,
                    MIN_BREAKER_OBSERVATION_GAP_SECONDS,
                    unchanged_count,
                )
                return unchanged_count

    return _record_breaker_observation(backend, fingerprint)


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
    breaker_backend = _get_breaker_backend(golden_repo_manager)

    if not _golden_repos_dir_is_healthy(golden_repo_manager.golden_repos_dir):
        # A base-dir health-check failure is direct evidence of real infra
        # instability -- it voids any prior circuit-breaker confirmations
        # (Bug #1382) so flapping infra can never accumulate toward the
        # confirmation threshold.
        _reset_breaker_state(breaker_backend)
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
    absent_fraction = (len(orphan_candidates) / total) if total > 0 else 0.0

    if total > 0 and absent_fraction > ORPHAN_FRACTION_ABORT_THRESHOLD:
        # Bug #1382: consult the persisted, cross-restart confirmation
        # counter before trusting the ratio-only "infra problem"
        # interpretation. Only a STABLE, repeated orphan-candidate set
        # observed across CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD consecutive
        # sweeps -- each with a healthy base directory (guaranteed by the
        # reset above) -- counts as corroborating evidence that this is
        # genuine orphans, not a blip.
        fingerprint = ",".join(sorted(orphan_candidates))
        confirmed_count = _record_breaker_observation_with_time_gate(
            breaker_backend, fingerprint
        )
        result.circuit_breaker_consecutive_count = confirmed_count

        if confirmed_count >= CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD:
            result.circuit_breaker_confirmed_proceed = True
            logger.warning(
                "Bug #1382 reconcile: circuit-breaker CONFIRMED -- the same "
                "%d/%d orphan-candidate set (%s) has now been observed on "
                "%d consecutive sweeps, each with a healthy "
                "golden_repos_dir -- treating as genuine persistent orphans "
                "(not an infra/mount blip) and proceeding with removal.",
                len(orphan_candidates),
                total,
                fingerprint,
                confirmed_count,
            )
            _reset_breaker_state(breaker_backend)
            # Fall through to Pass 2 below -- proceed exactly like a normal
            # within-threshold sweep.
        else:
            result.orphans_found = orphan_candidates
            result.aborted = True
            result.abort_reason = (
                f"refusing to reconcile: {len(orphan_candidates)}/{total} "
                "golden repos resolve absent -- likely an infra/mount "
                "problem, not orphans. (circuit-breaker confirmation: "
                f"{confirmed_count}/{CIRCUIT_BREAKER_CONFIRMATION_THRESHOLD} "
                "consecutive matching sweeps needed before auto-proceeding "
                "-- Bug #1382.)"
            )
            logger.warning("Bug #1317 reconcile: %s", result.abort_reason)
            return
    else:
        # Ratio within normal bounds this sweep (or empty registry) -- clear
        # any stale confirmation progress from a prior high-ratio streak.
        _reset_breaker_state(breaker_backend)

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
