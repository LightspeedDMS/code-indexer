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
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from code_indexer.global_repos.refresh_integrity_gate import (
    RefreshIntegrityGateResult,
    run_refresh_integrity_gate,
)
from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.services.index_failure_exit_codes import (
    ChunkStoreFailureKind,
    FatalChunkStoreIndexError,
)

if TYPE_CHECKING:
    from code_indexer.server.storage.protocols.golden_repo_metadata_backend import (
        GoldenRepoMetadataBackend,
    )

logger = logging.getLogger(__name__)

#: Backoff after the first unrepaired failure, doubling per further failure
#: up to the cap (same policy as Bug #1341's permanent fetch failures).
REFRESH_FAILURE_BACKOFF_BASE_SECONDS = 300
REFRESH_FAILURE_BACKOFF_CAP_SECONDS = 21600

#: Bug #1506: this many consecutive integrity-gate failures (strikes) for
#: one alias QUARANTINE it -- mirrors PROMPT_FAILURE_QUARANTINE_THRESHOLD and
#: FLEET_MIGRATION_FAILURE_QUARANTINE_THRESHOLD (both 3).
REFRESH_INTEGRITY_QUARANTINE_THRESHOLD = 3

#: Refresh results that prove the alias is healthy again.
_VERIFIED_SUCCESS_MESSAGES = frozenset({"Refresh complete", "No changes detected"})


def failure_backoff_seconds(consecutive_failures: int) -> int:
    exponent = max(0, consecutive_failures - 1)
    return int(
        min(
            REFRESH_FAILURE_BACKOFF_BASE_SECONDS * (2**exponent),
            REFRESH_FAILURE_BACKOFF_CAP_SECONDS,
        )
    )


def active_backoff_until(metadata: Any, alias_name: str) -> Optional[float]:
    """Epoch time until which system submissions of *alias_name* are
    deferred, or None. A store read failure propagates (fail closed)."""
    state = metadata.get_refresh_failure_backoff_state(alias_name)
    if state is None:
        return None
    until = float(state["last_failed_at"]) + failure_backoff_seconds(
        int(state["consecutive_failure_count"])
    )
    return until if until > time.time() else None


class RefreshDeferredError(DuplicateJobError):
    """A system refresh trigger deferred by the persisted failure backoff.
    A DuplicateJobError on purpose: every system caller already treats that
    as "a refresh will happen later -- retry/debounce", never as dropped."""

    def __init__(self, alias_name: str, backoff_until: float) -> None:
        Exception.__init__(
            self,
            f"refresh of {alias_name} deferred until {backoff_until:.0f}: "
            f"persisted failure backoff active",
        )
        self.operation_type = "global_repo_refresh"
        self.repo_alias = alias_name
        self.existing_job_id = ""
        self.backoff_until = backoff_until


def defer_if_backed_off(metadata: Any, alias_name: str) -> None:
    """Raise RefreshDeferredError when *alias_name* is inside its backoff."""
    backoff_until = active_backoff_until(metadata, alias_name)
    if backoff_until is not None:
        logger.info(f"Refresh for {alias_name} deferred until {backoff_until:.0f}")
        raise RefreshDeferredError(alias_name, backoff_until)


def record_failure_backoff(metadata: Any, alias_name: str, detail: str) -> None:
    """Persist one unrepaired failure; a persistence error is logged, never
    raised (the refresh is already failing with its own error)."""
    try:
        count = metadata.record_refresh_failure_backoff(alias_name, detail)
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


def clear_failure_backoff(metadata: Any, alias_name: str) -> None:
    try:
        metadata.reset_refresh_failure_backoff(alias_name)
    except Exception as exc:
        logger.error(
            f"Bug #2022: failed to clear refresh failure backoff for "
            f"{alias_name}: {type(exc).__name__}: {exc}"
        )


def clear_backoff_after_verified_success(
    metadata: Any, alias_name: str, result: Dict[str, Any]
) -> None:
    """A published refresh, or one that verified nothing needs indexing,
    ends the backoff. Skips (write lock held, not found, ...) do not."""
    if result.get("success") and result.get("message") in _VERIFIED_SUCCESS_MESSAGES:
        clear_failure_backoff(metadata, alias_name)


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


def run_integrity_gate_against_published(
    snapshot_manager: Any,
    source_path: str,
    current_target: Optional[str],
    before_restore: Optional[Callable[[], None]] = None,
) -> RefreshIntegrityGateResult:
    """Run the Bug #1506 gate on *source_path*'s index, self-healing a
    corrupt collection from the published snapshot at *current_target*
    (read only). No published snapshot yet (first refresh: the alias still
    points at the source) means nothing to restore from."""
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
