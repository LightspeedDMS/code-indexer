"""Refresh-failure recovery for fatal chunk-store failures (Bug #2022 Gap 4).

When ``cidx index`` fails on a fatal chunk-store error, the publish-time
integrity gate never runs, so this module owns what happens instead:

- self-heal: for a CORRUPTION failure, run the integrity gate against the
  published snapshot -- restoring only a collection whose check completed
  and reported damage, and only while the refresh still owns its write lock
  (re-checked immediately before every restore copy);
- strike: persisted via the caller's existing integrity-failure recorder,
  so the existing 3-strike quarantine applies;
- backoff: a persisted, per-alias, exponential and capped delay for
  failures the self-heal could not repair, honoured by every system
  submission and cleared by the next verified success.

State lives only in the golden-repo metadata store (SQLite solo, PostgreSQL
cluster); nothing here is per-node.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from code_indexer.global_repos.refresh_integrity_gate import (
    CollectionIntegrityFailure,
    RefreshIntegrityGateResult,
    run_refresh_integrity_gate,
)
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.services.index_failure_exit_codes import (
    ChunkStoreFailureKind,
    FatalChunkStoreIndexError,
)

if TYPE_CHECKING:
    from code_indexer.server.services.metadata_reader import RecordedFailures
    from code_indexer.server.storage.protocols.golden_repo_metadata_backend import (
        GoldenRepoMetadataBackend,
    )

logger = logging.getLogger(__name__)

#: Backoff after the first unrepaired failure, doubling per further failure
#: up to the cap. The single definition: RefreshScheduler.PERMANENT_BACKOFF_*
#: (Bug #1341 permanent fetch failures) derive from these.
FAILURE_BACKOFF_BASE_SECONDS = 300
FAILURE_BACKOFF_CAP_SECONDS = 21600

#: Bug #1506: this many consecutive integrity-gate failures (strikes) for
#: one alias QUARANTINE it -- mirrors PROMPT_FAILURE_QUARANTINE_THRESHOLD and
#: FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD (both 3).
REFRESH_INTEGRITY_QUARANTINE_THRESHOLD = 3


def failure_backoff_seconds(consecutive_failures: int) -> int:
    exponent = max(0, consecutive_failures - 1)
    return int(
        min(
            FAILURE_BACKOFF_BASE_SECONDS * (2**exponent),
            FAILURE_BACKOFF_CAP_SECONDS,
        )
    )


def active_backoff_until(metadata: Any, alias_name: str) -> Optional[float]:
    """Epoch time until which system submissions of *alias_name* are
    deferred, or None. A row with no failures left (re-armed by a verified
    publish to keep a pending trigger) defers nothing. A store read failure
    propagates (fail closed)."""
    state = metadata.get_refresh_failure_backoff_state(alias_name)
    if state is None or int(state["consecutive_failure_count"]) == 0:
        return None
    until = _backoff_end(state)
    return until if until > time.time() else None


def _backoff_end(state: Dict[str, Any]) -> float:
    return float(state["last_failed_at"]) + failure_backoff_seconds(
        int(state["consecutive_failure_count"])
    )


class RefreshDeferredError(DuplicateJobError):
    """A system refresh trigger deferred by the persisted failure backoff.
    A DuplicateJobError on purpose: every system caller already treats that
    as "a refresh will happen later -- retry/debounce", never as dropped."""

    def __init__(
        self,
        alias_name: str,
        backoff_until: float,
        reason: Optional[str] = None,
    ) -> None:
        # reason None: the persisted failure backoff (the original message).
        Exception.__init__(
            self,
            f"refresh of {alias_name} deferred until {backoff_until:.0f}: "
            f"{reason or 'persisted failure backoff active'}",
        )
        self.reason = reason
        self.operation_type = "global_repo_refresh"
        self.repo_alias = alias_name
        self.existing_job_id = ""
        self.backoff_until = backoff_until


def defer_if_backed_off(metadata: Any, alias_name: str) -> None:
    """Raise RefreshDeferredError when *alias_name* is inside its backoff,
    after persisting a pending trigger that fire_expired_deferred_triggers
    submits once the backoff ends (the trigger is deferred, never dropped).
    Returns normally -- the caller submits -- when the backoff row vanished
    between the read and the mark (a concurrent verified publish)."""
    backoff_until = active_backoff_until(metadata, alias_name)
    if backoff_until is not None:
        if not metadata.mark_refresh_trigger_pending(alias_name, backoff_until):
            logger.info(f"Backoff of {alias_name} resolved meanwhile; submitting")
            return
        logger.info(
            f"Refresh for {alias_name} deferred until {backoff_until:.0f} "
            f"(persisted failure backoff); it fires once the backoff ends"
        )
        raise RefreshDeferredError(alias_name, backoff_until)


def defer_due_alias(metadata: Any, registry: Any, alias_name: str) -> bool:
    """Git schedule: a due alias inside its backoff is deferred by moving its
    ``next_refresh`` to the backoff end. True when it was deferred."""
    backoff_until = active_backoff_until(metadata, alias_name)
    if backoff_until is None:
        return False
    registry.update_next_refresh(alias_name, backoff_until)
    return True


def _log_store_error(db_throttle: Any, exc: Exception, what: str) -> None:
    if not db_throttle.on_db_error(exc, logger):
        logger.error(f"Bug #2022: {what}: {type(exc).__name__}: {exc}")


def fire_expired_deferred_triggers(
    metadata: Any, submit: Callable[[str], Any], db_throttle: Any
) -> None:
    """Scheduler loop: submit each deferred system trigger that is due.

    The trigger is LEASED (an atomic store update, so one scheduler takes
    it) for the alias's backoff interval and stays pending: only a verified
    publish resolves it. Process death before or after the submission, an
    orphaned job or a failed submission therefore only delays it to the
    lease end, while a pass during an in-flight job neither writes nor logs.
    Errors are handled per alias (``db_throttle`` as in the due loop), so one
    row never fails the iteration. Index-backed: touches due triggers only.
    """
    now = time.time()
    try:
        due = metadata.list_due_refresh_triggers(now)
    except Exception as exc:
        _log_store_error(db_throttle, exc, "listing due deferred refreshes failed")
        return
    for state in due:
        alias_name = state["golden_alias"]
        # Bug #2125: an owed retry's lease follows its own count.
        lease_until = now + failure_backoff_seconds(
            max(
                int(state["consecutive_failure_count"]),
                int(state.get("owed_retry_count", 0)),
            )
        )
        try:
            if not metadata.lease_pending_refresh_trigger(alias_name, now, lease_until):
                continue  # another scheduler holds it
            submit(alias_name)
            db_throttle.on_db_success(logger)
        except DuplicateJobError as exc:
            # In flight already (dedup), or re-deferred by a newer backoff.
            logger.debug(f"Deferred refresh of {alias_name} not submitted: {exc}")
        except Exception as exc:
            _log_store_error(
                db_throttle,
                exc,
                f"deferred refresh of {alias_name} not submitted; "
                f"retried when its lease ends",
            )


def record_failure_backoff(
    metadata: Any,
    alias_name: str,
    detail: str,
    unrepairable_reason: Optional[str] = None,
) -> None:
    """Persist one unrepaired failure; a persistence error is logged, never
    raised (the refresh is already failing with its own error).
    *unrepairable_reason* (Story #2126): why no automatic repair can fix it;
    health reports it naming the repository until a verified publish."""
    try:
        if unrepairable_reason is None:
            count = metadata.record_refresh_failure_backoff(alias_name, detail)
        else:
            count = metadata.record_refresh_failure_backoff(
                alias_name, detail, unrepairable_reason=unrepairable_reason
            )
            logger.error(
                f"Story #2126: refresh of {alias_name} needs an operator: "
                f"{unrepairable_reason}"
            )
    except Exception as exc:
        logger.error(
            f"Bug #2022: failed to persist refresh failure backoff for "
            f"{alias_name}: {type(exc).__name__}: {exc}"
        )
        return
    logger.error(
        f"Bug #2022: {alias_name} refresh failed {count} consecutive time(s) "
        f"without repair -- system refreshes back off "
        f"{failure_backoff_seconds(count)}s"
    )


def schedule_owed_retry(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    detail: str,
    covered_generation: Optional[int],
    *,
    min_count: int = 0,
) -> Optional[float]:
    """Bug #2125: persist an OWED retry of *alias_name* -- work a refresh
    still owes, retried without waiting for new data. It is NOT a failure:
    ``consecutive_failure_count`` (what defers new-data triggers and the git
    schedule) is untouched; ``owed_retry_count`` escalates the interval
    instead (FAILURE_BACKOFF_BASE_SECONDS doubling to
    FAILURE_BACKOFF_CAP_SECONDS; never gives up). A trigger that arrived
    while the finishing cycle ran (generation newer than
    *covered_generation*) keeps its earlier due time. The pending trigger
    fires through fire_expired_deferred_triggers and the system gate
    (failure backoff, then the volume alarm); a clean verified publish
    resolves it. Returns the due time, or None when the store failed
    (logged at ERROR, never raised)."""
    try:
        count, due_at = metadata.record_owed_refresh_retry(
            alias_name, detail, covered_generation, min_count, failure_backoff_seconds
        )
    except Exception as exc:
        logger.error(
            f"Bug #2125: failed to persist the owed retry of {alias_name}: "
            f"{type(exc).__name__}: {exc}"
        )
        return None
    logger.info(
        f"Bug #2125: owed retry {count} of {alias_name} due at {due_at:.0f} "
        f"(in {max(0.0, due_at - time.time()):.0f}s): {detail}"
    )
    return float(due_at)


def retry_failed_trigger_only_refresh(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    detail: str,
    covered_generation: Optional[int],
    *,
    fatal_failure_recorded: bool,
) -> Optional[float]:
    """Bug #2125 (A): a trigger-only repo's refresh failed and nothing else
    would ever re-trigger it. *fatal_failure_recorded*: the failure was a
    fatal chunk-store failure, whose self-heal may have recorded a Bug #2022
    failure backoff (it does not when it repaired the store); when that
    backoff is active, the retry is a pending trigger at its end. In every
    other case -- an ordinary failure (a provider error costs nothing to
    retry, and must recover promptly once fixed) or a repaired store -- it
    is an owed retry, never a new failure. Returns the due time, or None
    when nothing was scheduled (logged)."""
    if fatal_failure_recorded:
        try:
            state = metadata.get_refresh_failure_backoff_state(alias_name)
            if state is not None and int(state["consecutive_failure_count"]) > 0:
                due_at = _backoff_end(state)
                if metadata.mark_refresh_trigger_pending(alias_name, due_at):
                    logger.warning(
                        f"Bug #2125: failed refresh of {alias_name} retried "
                        f"at {due_at:.0f}, the end of its failure backoff"
                    )
                    return due_at
        except Exception as exc:
            logger.error(
                f"Bug #2125: failed to schedule the retry of the failed "
                f"refresh of {alias_name}: {type(exc).__name__}: {exc}"
            )
            return None
    due = schedule_owed_retry(metadata, alias_name, detail, covered_generation)
    if due is not None:
        logger.warning(
            f"Bug #2125: failed refresh of {alias_name} retried at {due:.0f}"
        )
    return due


def settle_publish(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    covered_generation: int,
    failures: "RecordedFailures",
    *,
    schedule_retry: bool,
) -> Optional[float]:
    """Bookkeeping of every verified publish (replaces a bare
    resolve_after_publish). The publish always resolves what its cycle
    covered, exactly as before (Bug #2022 failure state included; a trigger
    that arrived mid-cycle stays, re-armed to fire at once). Bug #2125 (B):
    when *schedule_retry* and work is still owed -- the child recorded files
    that still exist (``failures.retryable_paths``), or its recorded
    failures are unknown (unreadable or malformed metadata, logged at
    WARNING) -- the resolution and an owed retry escalated from the alias's
    owed count are ONE store transaction (resolve_and_record_owed_retry): a
    store failure commits nothing, so the covered pending trigger survives
    and its lease fires it again. A stale counter or vanished files owe
    nothing. Returns the owed retry's due time, or None."""
    if not failures.known:
        logger.warning(
            f"Bug #2125: recorded indexing failures of {alias_name} are "
            f"unknown (unreadable metadata); retried as owed work"
        )
    owes = not failures.known or failures.retryable_paths > 0
    if not (owes and schedule_retry):
        resolve_after_publish(metadata, alias_name, covered_generation)
        return None
    detail = (
        f"{failures.retryable_paths} file(s) still owed"
        if failures.known
        else "recorded indexing failures unknown"
    )
    try:
        count, due_at = metadata.resolve_and_record_owed_retry(
            alias_name, covered_generation, detail, failure_backoff_seconds
        )
    except Exception as exc:
        logger.error(
            f"Bug #2125: failed to record the publish of {alias_name} with its "
            f"owed retry (nothing committed; its pending trigger fires again "
            f"at its lease end): {type(exc).__name__}: {exc}"
        )
        return None
    logger.info(
        f"Bug #2125: owed retry {count} of {alias_name} due at {due_at:.0f}: {detail}"
    )
    return float(due_at)


#: Trigger generation of an alias with no backoff row (the column default).
NO_TRIGGER_GENERATION = 0


def begin_refresh_cycle(
    metadata: "GoldenRepoMetadataBackend", alias_name: str
) -> Tuple[bool, int]:
    """(regate, covered_generation) for a refresh cycle, read from the store
    before the cycle reads its source.

    regate: a failure no verified publish has resolved yet -- a persisted
    backoff (unrepaired failure or inconclusive check) or integrity strikes;
    such a cycle must re-gate and publish, never take a "No changes
    detected" shortcut. covered_generation: the row's trigger generation;
    every later deferral advances it, so resolve_after_publish keeps that
    trigger. Store-ordered, never a wall clock (nodes' clocks differ). A
    store read failure propagates."""
    state = metadata.get_refresh_failure_backoff_state(alias_name)
    covered_generation = (
        NO_TRIGGER_GENERATION if state is None else int(state["trigger_generation"])
    )
    regate = (
        state is not None
        or metadata.get_refresh_integrity_failure_state(alias_name) is not None
    )
    return regate, covered_generation


def resolve_after_publish(
    metadata: "GoldenRepoMetadataBackend", alias_name: str, covered_generation: int
) -> None:
    """A verified publish resolves the alias's failure backoff and every
    deferred trigger its cycle covered (*covered_generation*); a trigger
    deferred during the cycle stays pending, re-armed to fire at once. The
    only resolution point: while a row exists, a cycle re-gates and cannot
    end as "No changes detected". A store error is logged, never raised:
    the alias is already published."""
    try:
        metadata.resolve_refresh_failure_backoff(alias_name, covered_generation)
    except Exception as exc:
        logger.error(
            f"Bug #2022: failed to resolve the refresh failure backoff of "
            f"{alias_name} after publishing: {type(exc).__name__}: {exc}"
        )


#: Refresh results no refresh can get past without an operator or a new
#: registration: integrity or local-repair quarantine, an orphaned clone,
#: and an alias or registry entry that no longer exists.
UNSERVICEABLE_SKIPS = frozenset(
    {"integrity_quarantined", "local_repo_repair_quarantined"}
)
UNSERVICEABLE_MESSAGES = frozenset(
    {
        "Orphaned golden repo (clone missing), skipped",
        "Alias not found, skipped",
        "Repo not in registry, skipped",
    }
)
#: Results that settled the alias's state themselves (a publish, a verified
#: no-change, or an integrity-gate failure recording its own strike/backoff).
SELF_SETTLED_MESSAGES = frozenset(
    {
        "Refresh complete",
        "No changes detected",
        "Refresh integrity gate failed; publish skipped",
    }
)


def current_trigger_generation(
    metadata: "GoldenRepoMetadataBackend", alias_name: str
) -> Optional[int]:
    """The alias's trigger generation now (NO_TRIGGER_GENERATION: no row),
    read before a refresh runs so the cycle knows which trigger it covers.
    None when the store read failed (logged): a skip then drops nothing, and
    an owed retry keeps any earlier pending trigger's due time."""
    try:
        state = metadata.get_refresh_failure_backoff_state(alias_name)
    except Exception as exc:
        logger.error(
            f"Bug #2022: failed to read the trigger generation of {alias_name}; "
            f"a skip of this refresh drops no trigger: {type(exc).__name__}: {exc}"
        )
        return None
    return NO_TRIGGER_GENERATION if state is None else int(state["trigger_generation"])


def settle_skip(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    covered_generation: Optional[int],
    result: Dict[str, Any],
) -> None:
    """Settle a refresh that returned without publishing, so a deferred
    trigger does not re-fire forever as a job that does nothing. An
    unserviceable skip drops the trigger -- only the generation that existed
    before the refresh ran (*covered_generation*), so a newer one survives.
    A self-settled result needs nothing. Any other skip is recoverable (held
    write lock, uninitialized local repo, unreadable quarantine state, a
    failed local repair): the trigger stays, with one more failure so its
    retry interval escalates (bounded by the backoff cap) -- one
    generation-conditional store statement that keeps the original failure
    reason and never recreates a row a publish deleted. A store error is
    logged, never raised."""
    message = result.get("message")
    try:
        if (
            result.get("skipped") in UNSERVICEABLE_SKIPS
            or message in UNSERVICEABLE_MESSAGES
        ):
            if covered_generation is not None and metadata.clear_refresh_trigger(
                alias_name, covered_generation
            ):
                logger.warning(
                    f"Bug #2022: deferred refresh of {alias_name} dropped: "
                    f"{message} (needs an operator)"
                )
        elif message not in SELF_SETTLED_MESSAGES and covered_generation is not None:
            count = metadata.escalate_refresh_trigger(alias_name, covered_generation)
            if count is not None:
                logger.warning(
                    f"Bug #2022: deferred refresh of {alias_name} skipped "
                    f"({message}); retried in {failure_backoff_seconds(count)}s"
                )
    except Exception as exc:
        logger.error(
            f"Bug #2022: failed to settle the skipped refresh of {alias_name}: "
            f"{type(exc).__name__}: {exc}"
        )


def record_integrity_strike(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    gate_result: RefreshIntegrityGateResult,
) -> None:
    """Log + persist a Bug #1506 integrity-gate failure (one strike toward
    the quarantine). A persistence error is logged, never raised."""
    detail_summary = "; ".join(
        f"{f.collection_dir}: {f.detail}" for f in gate_result.failures
    )
    logger.error(
        f"Bug #1506: refusing to publish refresh for "
        f"{alias_name} -- integrity gate failed for "
        f"{len(gate_result.failures)} collection(s): "
        f"{detail_summary}. The already-published alias "
        f"continues serving the last verified-good snapshot."
    )
    try:
        failure_count = metadata.record_refresh_integrity_failure(
            alias_name, detail_summary
        )
        if failure_count >= REFRESH_INTEGRITY_QUARANTINE_THRESHOLD:
            logger.error(
                f"Bug #1506: {alias_name} has failed the refresh "
                f"integrity gate {failure_count} consecutive times -- "
                f"QUARANTINED. Operator attention is required to "
                f"investigate the underlying corruption source."
            )
    except Exception as quarantine_exc:
        logger.error(
            f"Bug #1506: failed to record refresh-integrity "
            f"quarantine state for {alias_name} (non-fatal): "
            f"{type(quarantine_exc).__name__}: {quarantine_exc}"
        )


def reset_integrity_strikes(
    metadata: "GoldenRepoMetadataBackend", alias_name: str
) -> None:
    """Clear prior Bug #1506 quarantine state on a gate pass. A failure is
    logged at ERROR (Bug #1506 4th-pass review Item 3): a reset that keeps
    failing silently confuses future quarantine decisions."""
    try:
        metadata.reset_refresh_integrity_failure(alias_name)
    except Exception as reset_exc:
        logger.error(
            f"Bug #1506: failed to reset refresh-integrity quarantine "
            f"state for {alias_name} (non-fatal): "
            f"{type(reset_exc).__name__}: {reset_exc}"
        )


def record_publish_gate_failure(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    gate_result: RefreshIntegrityGateResult,
) -> None:
    """Bookkeeping for a failed publish-time gate: a strike only for confirmed
    corruption; an inconclusive check (it could not run) backs off instead,
    so transient I/O never quarantines a healthy repo."""
    confirmed = [f for f in gate_result.failures if not f.check_inconclusive]
    if confirmed:
        record_integrity_strike(metadata, alias_name, gate_result)
    if len(confirmed) < len(gate_result.failures):
        record_failure_backoff(
            metadata, alias_name, "publish integrity check inconclusive"
        )


def run_integrity_gate_against_published(
    snapshot_manager: Any,
    source_path: str,
    current_target: Optional[str],
    before_restore: Optional[Callable[[], None]] = None,
    restore_missing_stores: bool = False,
) -> RefreshIntegrityGateResult:
    """Run the Bug #1506 gate on *source_path*'s index, self-healing a
    corrupt collection -- and, with *restore_missing_stores* (Story #2126),
    an established collection whose store is missing -- from the published
    snapshot at *current_target* (read only). No published snapshot yet
    (first refresh: the alias still points at the source) means nothing to
    restore from."""
    healthy_index_dir = (
        Path(current_target) / ".code-indexer" / "index"
        if current_target and current_target != source_path
        else None
    )
    clone_backend = (
        getattr(snapshot_manager, "_clone_backend", None)
        if snapshot_manager is not None
        else None
    )
    return run_refresh_integrity_gate(
        source_index_dir=Path(source_path) / ".code-indexer" / "index",
        healthy_index_dir=healthy_index_dir,
        clone_backend=clone_backend,
        before_restore=before_restore,
        restore_missing_stores=restore_missing_stores,
    )


def self_heal_after_fatal_chunk_store_failure(
    *,
    metadata: "GoldenRepoMetadataBackend",
    snapshot_manager: Any,
    alias_name: str,
    source_path: str,
    current_target: Optional[str],
    error: FatalChunkStoreIndexError,
    verify_ownership: Callable[[], None],
) -> None:
    """``cidx index`` failed on a fatal chunk-store error. Called while the
    refresh still holds its publish write lock; never publishes (the caller
    re-raises *error*). *verify_ownership* raises when the lock is lost; it
    runs first and again immediately before every restore copy."""
    verify_ownership()
    if error.kind is ChunkStoreFailureKind.MISSING_STORE:
        _restore_missing_stores(
            metadata=metadata,
            snapshot_manager=snapshot_manager,
            alias_name=alias_name,
            source_path=source_path,
            current_target=current_target,
            error=error,
            verify_ownership=verify_ownership,
        )
        return
    if error.kind is not ChunkStoreFailureKind.CORRUPTION:
        logger.error(
            f"Bug #2022: refresh of {alias_name} failed with a chunk-store "
            f"{error.kind.value} failure (I/O, lock, full, read-only or "
            f"permission) -- not corruption, nothing restored: {error}"
        )
        record_failure_backoff(metadata, alias_name, str(error))
        return
    gate = run_integrity_gate_against_published(
        snapshot_manager, source_path, current_target, before_restore=verify_ownership
    )
    if gate.passed:
        logger.error(
            f"Bug #2022: cidx index reported chunk-store corruption for "
            f"{alias_name} but every collection passes integrity_check -- "
            f"nothing restored: {error}"
        )
        record_failure_backoff(metadata, alias_name, str(error))
        return
    confirmed = [f for f in gate.failures if not f.check_inconclusive]
    if confirmed:
        record_integrity_strike(metadata, alias_name, gate)
    repaired = len(confirmed) == len(gate.failures) and all(
        f.self_heal_succeeded and f.metadata_restore_error is None for f in confirmed
    )
    if not repaired:
        record_failure_backoff(metadata, alias_name, str(error))


def _restore_missing_stores(
    *,
    metadata: "GoldenRepoMetadataBackend",
    snapshot_manager: Any,
    alias_name: str,
    source_path: str,
    current_target: Optional[str],
    error: FatalChunkStoreIndexError,
    verify_ownership: Callable[[], None],
) -> None:
    """Story #2126: an established collection's store is missing. Restore it
    -- with its metadata, so the next run resumes from the snapshot's commit
    and reuses every stored embedding -- from the published snapshot through
    the Bug #2022 path: the snapshot copy is integrity-checked read-only
    first, and *verify_ownership* runs immediately before every copy. One
    strike per cycle bounds repeated loss (3 without a verified publish
    quarantine the alias); anything left unrepaired backs off."""
    gate = run_integrity_gate_against_published(
        snapshot_manager,
        source_path,
        current_target,
        before_restore=verify_ownership,
        restore_missing_stores=True,
    )
    missing = [f for f in gate.failures if f.store_missing]
    corrupt = [
        f for f in gate.failures if not f.store_missing and not f.check_inconclusive
    ]
    restored = [f for f in missing if f.self_heal_succeeded]
    if corrupt:
        record_integrity_strike(metadata, alias_name, gate)
    elif restored:
        _record_missing_store_strike(metadata, alias_name, restored)
    repaired = bool(missing) and all(
        f.self_heal_succeeded and f.metadata_restore_error is None
        for f in gate.failures
    )
    if not repaired:
        record_failure_backoff(
            metadata,
            alias_name,
            str(error),
            unrepairable_reason=_unrestorable_reason(
                source_path, current_target, missing
            ),
        )


def _unrestorable_reason(
    source_path: str,
    current_target: Optional[str],
    missing: List[CollectionIntegrityFailure],
) -> Optional[str]:
    """Why a missing store cannot be restored without an operator, or None
    when every missing store was restored (whatever else failed is an
    ordinary, retried failure)."""
    if not missing:
        return (
            "cidx index reported a missing chunk store, but no collection "
            "under the repository index is missing one (nothing to restore "
            "from the published snapshot)"
        )
    unrestored = [f for f in missing if not f.self_heal_succeeded]
    if not unrestored:
        return None
    no_snapshot = not current_target or current_target == source_path
    causes = []
    for failure in unrestored:
        if no_snapshot:
            why = "no published snapshot to restore it from"
        elif failure.source_snapshot_also_corrupt:
            why = "its copy in the published snapshot failed its integrity check"
        elif not failure.self_heal_attempted:
            why = "the published snapshot has no copy of it"
        else:
            why = f"the restore failed: {failure.self_heal_error}"
        causes.append(f"{failure.collection_dir.name}: {why}")
    return (
        f"chunk store missing and not restorable ({'; '.join(causes)}); the "
        f"refresh stays failed -- no paid rebuild without an operator"
    )


def _record_missing_store_strike(
    metadata: "GoldenRepoMetadataBackend",
    alias_name: str,
    restored: List[CollectionIntegrityFailure],
) -> None:
    """One strike for a cycle that restored a missing store: a verified
    publish clears it, while a store lost again and again quarantines the
    alias after REFRESH_INTEGRITY_QUARANTINE_THRESHOLD restores. A
    persistence error is logged, never raised."""
    detail = "; ".join(
        f"{f.collection_dir}: chunks.db missing, restored from the published snapshot"
        for f in restored
    )
    try:
        count = metadata.record_refresh_integrity_failure(alias_name, detail)
    except Exception as exc:
        logger.error(
            f"Story #2126: failed to record the missing-store restore of "
            f"{alias_name}: {type(exc).__name__}: {exc}"
        )
        return
    logger.warning(
        f"Story #2126: restored the missing chunk store of {alias_name} from "
        f"its published snapshot (restore {count} without a verified publish); "
        f"the next refresh reuses every stored embedding: {detail}"
    )
    if count >= REFRESH_INTEGRITY_QUARANTINE_THRESHOLD:
        record_failure_backoff(
            metadata,
            alias_name,
            detail,
            unrepairable_reason=(
                f"lost its chunk store {count} consecutive times without a "
                f"verified publish (each restored from the published "
                f"snapshot); refreshes are quarantined until an operator "
                f"finds what removes it"
            ),
        )
