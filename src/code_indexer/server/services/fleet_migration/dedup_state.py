"""Story #1560: duplicate-point-id auto-resolution outcome state.

Thin wrapper around the shared backend's record_dedup_outcome/
get_dedup_state/list_dedup_states/clear_dedup_state methods
(GoldenRepoMetadataSqliteBackend / GoldenRepoMetadataPostgresBackend),
mirroring quarantine.py's own _get_quarantine_backend injection
convention exactly (Messi Rule #4: anti-duplication).

The `backend is None or not hasattr(backend, "...")` guard on every
function below is copied verbatim from quarantine.py's own established
functions (record_migration_failure, reset_migration_failure,
get_failure_state, count_quarantined). golden_repo_manager: Any mirrors
quarantine.py's own identical parameter typing throughout that file
(a duck-typed manager object with no shared Protocol defined anywhere
in this codebase). `golden_alias` is validated AND NORMALIZED (Codex
review Finding F7 -- "foo" and "foo-global" must resolve to the SAME
row) at THIS wrapper boundary, in addition to the backend layer's own
validation; `reason` is validated only (it is free-form text, not an
alias, so there is nothing to normalize).

`sweep_pending_dedup_outcomes_for_candidate()` is the AC22/AC23
integration point: it is called by FleetMigrationScheduler for EVERY
enumerated candidate (see scheduler.py's per-candidate loop), reading
collection_dedup_repair.py's crash-durable pending-outcome journal and
persisting it here, clearing the journal ONLY when a backend genuinely
persisted it (never when tracking is merely disabled -- that must
leave the journal in place rather than silently discard it).

Codex review Finding F1: a journal is only ever persisted+cleared when
its `phase` is "completed" -- filesystem-proven, not merely intended.
A crash can land between the journal write (BEFORE deletion) and the
first file actually being deleted; if this sweep blindly trusted and
cleared that journal, the interrupted repair pass would later retry,
finish the SAME deletion for real, and record its OWN "completed"
outcome for the SAME duplicates -- double-counting
records_deleted/duplicate_groups on /health. A `phase == "pending"`
journal (or one missing the field entirely, a legacy/malformed shape)
is therefore left UNTOUCHED here; it is resolved by a LATER repair
pass, which either flips it to "completed" once the filesystem proves
the intended deletion actually happened, or supersedes it with a
fresh, accurate recomputation (see collection_dedup_repair.py's
`_accumulate_pending_outcome_durably`/`_mark_pending_outcome_completed_durably`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from code_indexer.server.services.fleet_migration.alias_normalization import (
    normalize_golden_alias,
)

if TYPE_CHECKING:
    from code_indexer.server.services.fleet_migration.discovery import (
        FleetMigrationCandidate,
    )

logger = logging.getLogger(__name__)

#: Codex review Finding F1 hardening: a "completed"-phase journal MUST
#: carry these keys -- a malformed one (however that could happen) is
#: treated the SAME way as a "pending" phase (left untouched, logged)
#: rather than risking an uncaught KeyError mid-sweep.
_REQUIRED_JOURNAL_KEYS = (
    "duplicate_groups",
    "records_before",
    "records_deleted",
    "winner_kept_groups",
    "whole_group_deleted_groups",
    "collection_total",
)


class DedupStateUnavailableError(Exception):
    """A configured backend genuinely FAILED a dedup-state READ or
    WRITE. Mirrors quarantine.py's QuarantineStateUnavailableError:
    never silently swallowed (Story #1560 AC23)."""


def _get_dedup_backend(golden_repo_manager: Any) -> Optional[Any]:
    """The SAME shared backend GoldenRepoManager already uses for its
    registry rows. None if unavailable -- callers degrade to "tracking
    disabled" rather than raising."""
    return getattr(golden_repo_manager, "_sqlite_backend", None)


def _validate_and_normalize_alias(golden_alias: str) -> str:
    """Codex review Finding F7: validates AND normalizes `golden_alias`
    to its bare form (strips a trailing "-global" suffix if present) so
    every read/write below is keyed identically regardless of which
    form the caller passed -- "foo" and "foo-global" must never become
    two separate rows."""
    if not isinstance(golden_alias, str) or not golden_alias:
        raise ValueError(
            f"golden_alias must be a non-empty string, got {golden_alias!r}"
        )
    return str(normalize_golden_alias(golden_alias))


def record_dedup_outcome(
    golden_repo_manager: Any,
    golden_alias: str,
    *,
    duplicate_groups: int,
    records_before: int,
    records_deleted: int,
    winner_kept_groups: int,
    whole_group_deleted_groups: int,
    collection_total: int,
) -> Optional[Dict[str, Any]]:
    """Record one dedup-resolution outcome (AC6/AC7). Returns None if no
    backend is configured (deliberate "tracking disabled" no-op) --
    callers that need to distinguish "persisted" from "not persisted"
    must check for a non-None return, never assume success.

    Raises:
        ValueError: golden_alias is not a non-empty string.
        DedupStateUnavailableError: the WRITE genuinely failed (AC23).
    """
    golden_alias = _validate_and_normalize_alias(golden_alias)
    backend = _get_dedup_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "record_dedup_outcome"):
        return None
    try:
        return backend.record_dedup_outcome(  # type: ignore[no-any-return]
            golden_alias,
            duplicate_groups=duplicate_groups,
            records_before=records_before,
            records_deleted=records_deleted,
            winner_kept_groups=winner_kept_groups,
            whole_group_deleted_groups=whole_group_deleted_groups,
            collection_total=collection_total,
        )
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Story #1560: failed to PERSIST a dedup outcome for %r -- the "
            "filesystem deletion already happened; without this write the "
            "audit record would be silently lost: %s",
            golden_alias,
            exc,
        )
        raise DedupStateUnavailableError(
            f"failed to persist dedup outcome for {golden_alias!r}: {exc}"
        ) from exc


def get_dedup_state(
    golden_repo_manager: Any, golden_alias: str
) -> Optional[Dict[str, Any]]:
    """Currently persisted dedup-outcome state, or None if absent/no
    backend.

    Raises:
        ValueError: golden_alias is not a non-empty string.
        DedupStateUnavailableError: the READ genuinely failed.
    """
    golden_alias = _validate_and_normalize_alias(golden_alias)
    backend = _get_dedup_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "get_dedup_state"):
        return None
    try:
        return backend.get_dedup_state(golden_alias)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Story #1560: dedup state for %r is UNAVAILABLE: %s",
            golden_alias,
            exc,
        )
        raise DedupStateUnavailableError(
            f"failed to read dedup state for {golden_alias!r}: {exc}"
        ) from exc


def list_dedup_states(golden_repo_manager: Any) -> List[Dict[str, Any]]:
    """Every persisted dedup-outcome row -- used by the /health surface
    (AC13-AC18). Empty list if no backend.

    Raises:
        DedupStateUnavailableError: the READ genuinely failed.
    """
    backend = _get_dedup_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "list_dedup_states"):
        return []
    try:
        return backend.list_dedup_states()  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error("Story #1560: failed to list dedup states: %s", exc)
        raise DedupStateUnavailableError(f"failed to list dedup states: {exc}") from exc


def clear_dedup_state(golden_repo_manager: Any, golden_alias: str, reason: str) -> None:
    """Mark a dedup-outcome state as cleared (AC8). No-op if no backend.

    Raises:
        ValueError: golden_alias is not a non-empty string, or reason
            is not a non-empty string.
        DedupStateUnavailableError: the WRITE genuinely failed.
    """
    golden_alias = _validate_and_normalize_alias(golden_alias)
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"reason must be a non-empty string, got {reason!r}")
    backend = _get_dedup_backend(golden_repo_manager)
    if backend is None or not hasattr(backend, "clear_dedup_state"):
        return
    try:
        backend.clear_dedup_state(golden_alias, reason)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a typed error below
        logger.error(
            "Story #1560: failed to CLEAR dedup state for %r: %s",
            golden_alias,
            exc,
        )
        raise DedupStateUnavailableError(
            f"failed to clear dedup state for {golden_alias!r}: {exc}"
        ) from exc


def _is_well_formed_completed_journal(journal: Dict[str, Any]) -> bool:
    """Codex review Finding F1 hardening: True iff every key
    `record_dedup_outcome()` requires is present -- a malformed
    "completed"-phase journal (however that could happen) must never
    raise an uncaught KeyError mid-sweep."""
    return all(key in journal for key in _REQUIRED_JOURNAL_KEYS)


def _sweep_one_collection(
    golden_repo_manager: Any, golden_alias: str, collection_dir: Any
) -> None:
    """One collection's worth of sweep work -- extracted so the public
    entry point below stays within this codebase's function-length
    convention. See the module docstring (Codex review Finding F1) for
    why a non-"completed" phase is left untouched."""
    from code_indexer.storage.shared.collection_dedup_repair import (
        clear_pending_dedup_outcome,
        read_pending_dedup_outcome,
    )

    journal = read_pending_dedup_outcome(collection_dir)
    if journal is None:
        return
    if journal.get("phase") != "completed" or not _is_well_formed_completed_journal(
        journal
    ):
        logger.info(
            "Story #1560 Finding F1: leaving an unconfirmed or malformed "
            "('%s') dedup-outcome journal in place for %s -- a later "
            "repair pass resolves this, never this sweep.",
            journal.get("phase"),
            collection_dir,
        )
        return
    persisted = record_dedup_outcome(
        golden_repo_manager,
        golden_alias,
        duplicate_groups=journal["duplicate_groups"],
        records_before=journal["records_before"],
        records_deleted=journal["records_deleted"],
        winner_kept_groups=journal["winner_kept_groups"],
        whole_group_deleted_groups=journal["whole_group_deleted_groups"],
        collection_total=journal["collection_total"],
    )
    # Only clear when a backend genuinely persisted this outcome -- None
    # means "tracking disabled" (no backend configured), and discarding
    # the journal in that case would silently lose the audit trail
    # forever with no way to recover it later.
    if persisted is not None:
        clear_pending_dedup_outcome(collection_dir)


def sweep_pending_dedup_outcomes_for_candidate(
    golden_repo_manager: Any, candidate: "FleetMigrationCandidate"
) -> None:
    """Story #1560 AC22/AC23: for each of `candidate.semantic_collection_dirs`,
    check for a crash-durable pending dedup-outcome journal and, if its
    `phase` is "completed" (Codex review Finding F1), persist+clear it.
    See the module docstring for the full rationale.

    Called for EVERY enumerated candidate (not just the one actually
    attempted this scheduling tick) so a leftover journal from an OLDER
    crash -- possibly on a collection that has since fully migrated and
    is now skipped by is_repo_already_migrated() -- is never
    permanently orphaned.

    Raises:
        DedupStateUnavailableError: propagated unchanged from
            record_dedup_outcome() on a genuine backend WRITE failure
            (AC23) -- the journal is deliberately left in place,
            un-cleared, for a later retry. The caller must treat this
            as "data changed, state unavailable" and abort the tick
            rather than silently proceeding.
    """
    if candidate is None:
        raise ValueError("candidate must not be None")
    for collection_dir in candidate.semantic_collection_dirs:
        _sweep_one_collection(
            golden_repo_manager, candidate.golden_alias, collection_dir
        )
