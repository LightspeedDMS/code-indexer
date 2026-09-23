"""
Extra-methods mixin for GoldenRepoMetadataSqliteBackend.

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
This file exists solely because GoldenRepoMetadataSqliteBackend's original
body (1,423 lines) exceeds the project's 1,000-line-per-file limit even
after being moved into its own module; the class is composed via plain
multiple inheritance from golden_repo_metadata_backend.py, which owns
__init__ and the connection manager these methods use via `self._conn_manager`.
"""

import json
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..database_manager import DatabaseConnectionManager

# Issue #1383: golden_repo_reconcile_auto_heal_event is a singleton-row
# table (like golden_repo_reconcile_breaker_state) -- only the most recent
# confirmed auto-removal event needs to be discoverable.
_RECONCILE_AUTO_HEAL_EVENT_ROW_ID = 1


def _dedup_state_row_to_dict(row: Any) -> Dict[str, Any]:
    """Story #1560: map one ``fleet_migration_dedup_state`` row (SQLite
    tuple, column order matching every SELECT in
    :class:`GoldenRepoMetadataSqliteBackend`'s dedup-state methods) into
    the dict shape callers (the /health surface, dedup_state.py) expect.
    Shared by ``record_dedup_outcome``/``get_dedup_state``/
    ``list_dedup_states`` to avoid triplicating this field mapping."""
    return {
        "golden_alias": row[0],
        "duplicate_groups": row[1],
        "records_before": row[2],
        "records_deleted": row[3],
        "winner_kept_groups": row[4],
        "whole_group_deleted_groups": row[5],
        "collection_total": row[6],
        "first_dropped_at": row[7],
        "dropped_at": row[8],
        "cleared_at": row[9],
        "cleared_reason": row[10],
    }


class _GoldenRepoMetadataExtraMixin:
    """Extra methods for GoldenRepoMetadataSqliteBackend (see module docstring)."""

    # Type-only declaration: this mixin's methods use self._conn_manager,
    # which is genuinely supplied at runtime by
    # GoldenRepoMetadataSqliteBackend.__init__ (golden_repo_metadata_backend.py)
    # via composition -- mypy cannot see across the mixin boundary without it.
    _conn_manager: "DatabaseConnectionManager"

    def invalidate_dependency_map_tracking(self, alias: str) -> None:
        """
        Remove alias entry from dependency_map_tracking.commit_hashes JSON (Story #303).

        The commit_hashes column stores a JSON object mapping aliases to commit hashes.
        This removes the entry for the specified alias so the next analysis re-processes it.
        No-op if no tracking record exists or alias not in commit_hashes.
        """
        import json as _json

        def operation(conn):
            row = conn.execute(
                "SELECT commit_hashes FROM dependency_map_tracking WHERE id = 1"
            ).fetchone()
            if not row or not row[0]:
                return
            try:
                hashes = _json.loads(row[0])
            except (ValueError, TypeError):
                return
            if alias not in hashes:
                return
            del hashes[alias]
            conn.execute(
                "UPDATE dependency_map_tracking SET commit_hashes = ? WHERE id = 1",
                (_json.dumps(hashes),),
            )

        self._conn_manager.execute_atomic(operation)

    def list_repos_with_categories(self) -> List[Dict[str, Any]]:
        """
        List all golden repositories with category information (Story #181).

        Returns:
            List of repository dictionaries including category_id and category_auto_assigned.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            """SELECT alias, repo_url, default_branch, clone_path, created_at,
                      enable_temporal, temporal_options, category_id, category_auto_assigned,
                      COALESCE(wiki_enabled, 0)
               FROM golden_repos_metadata"""
        )

        result = []
        for row in cursor.fetchall():
            result.append(
                {
                    "alias": row[0],
                    "repo_url": row[1],
                    "default_branch": row[2],
                    "clone_path": row[3],
                    "created_at": row[4],
                    "enable_temporal": bool(row[5]),
                    "temporal_options": json.loads(row[6]) if row[6] else None,
                    "category_id": row[7],
                    "category_auto_assigned": bool(row[8]),
                    "wiki_enabled": bool(row[9]),
                }
            )

        return result

    def record_reconcile_breaker_observation(self, fingerprint: str) -> int:
        """
        Record one registry-reconcile circuit-breaker high-ratio observation
        (Bug #1382).

        If `fingerprint` (a stable, sorted identifier of the orphan-
        candidate alias set) matches the previously recorded fingerprint,
        the consecutive-observation count is incremented; otherwise (first
        observation ever, or a DIFFERENT orphan set than last time) the
        count resets to 1. Only a STABLE, repeated orphan-candidate set
        across sweeps is corroborating evidence that a high absent-fraction
        reflects real orphans rather than a one-off infra blip.

        Returns:
            The consecutive-observation count after recording this one.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            row = conn.execute(
                "SELECT orphan_fingerprint, consecutive_count "
                "FROM golden_repo_reconcile_breaker_state WHERE id = 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO golden_repo_reconcile_breaker_state "
                    "(id, orphan_fingerprint, consecutive_count, "
                    "first_observed_at, last_observed_at, updated_at) "
                    "VALUES (1, ?, 1, ?, ?, ?)",
                    (fingerprint, now, now, now),
                )
                return 1

            prev_fingerprint, prev_count = row
            if prev_fingerprint == fingerprint:
                new_count = prev_count + 1
                conn.execute(
                    "UPDATE golden_repo_reconcile_breaker_state "
                    "SET consecutive_count = ?, last_observed_at = ?, "
                    "updated_at = ? WHERE id = 1",
                    (new_count, now, now),
                )
                return new_count

            conn.execute(
                "UPDATE golden_repo_reconcile_breaker_state "
                "SET orphan_fingerprint = ?, consecutive_count = 1, "
                "first_observed_at = ?, last_observed_at = ?, updated_at = ? "
                "WHERE id = 1",
                (fingerprint, now, now, now),
            )
            return 1

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def reset_reconcile_breaker_state(self) -> None:
        """
        Clear the registry-reconcile circuit-breaker's persisted
        confirmation state (Bug #1382).

        Called whenever a sweep observes evidence AGAINST a stable-orphan
        hypothesis: a normal (non-tripping) sweep, or a base-directory
        health-check failure -- real infra flapping voids any prior
        confirmations, since the whole point is to only auto-proceed when
        the high ratio is consistently observed with a HEALTHY base
        directory every single time.
        """

        def operation(conn):
            conn.execute("DELETE FROM golden_repo_reconcile_breaker_state WHERE id = 1")

        self._conn_manager.execute_atomic(operation)

    def get_reconcile_auto_heal_event(self) -> Optional[Dict[str, Any]]:
        """
        Return the most recently persisted registry-reconcile auto-heal
        event, or None if no confirmed auto-removal has ever fired (Issue
        #1383).

        Used as an independently queryable discovery surface -- reused by
        HealthCheckService.get_golden_repo_reconcile_auto_heal_event() --
        so this historical event is discoverable WITHOUT log-searching,
        even after the breaker-state counter has been reset.
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            "SELECT removed_aliases, occurred_at FROM "
            "golden_repo_reconcile_auto_heal_event WHERE id = ?",
            (_RECONCILE_AUTO_HEAL_EVENT_ROW_ID,),
        ).fetchone()
        if row is None:
            return None
        removed_aliases_csv, occurred_at = row
        removed_aliases = [a for a in (removed_aliases_csv or "").split(",") if a]
        return {"removed_aliases": removed_aliases, "occurred_at": occurred_at}

    def record_fleet_migration_failure(
        self,
        golden_alias: str,
        state_signature: str,
        failure_cause: Optional[str] = None,
    ) -> int:
        """
        Record one fleet-migration consolidation failure for a golden repo
        (Issue #1477).

        The stored ``state_signature`` is ALWAYS overwritten to the value
        supplied for THIS failure (a cheap, non-recursive fingerprint of
        the repo's on-disk collection/temporal state at the moment of this
        failure) -- this is what lets a later scheduling attempt detect a
        GENUINE on-disk state change since the last failure, mirroring
        description_refresh_scheduler.py's commit-based quarantine
        auto-clear gate (Bug #1096).

        ``failure_cause`` (Finding I, Codex round-5 review) is ALSO always
        overwritten to the value supplied for THIS failure -- e.g.
        "disk_headroom" vs "generic" -- so ``is_quarantined()`` can
        distinguish a disk-headroom-caused quarantine (clears via a
        disk-space oracle, independent of directory content) from a
        corrupt-data-caused one (clears via the signature).

        Returns:
            The consecutive-failure count after recording this one.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            row = conn.execute(
                "SELECT consecutive_failure_count "
                "FROM fleet_migration_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            if row is None:
                # Issue #1477 Finding C (Codex round-3 review): every
                # column bound via an explicit placeholder (no inline
                # literal mixed with "?" markers) -- 8 columns, 8 "?", 8
                # tuple elements, unambiguous to verify by eye.
                conn.execute(
                    "INSERT INTO fleet_migration_quarantine_state "
                    "(golden_alias, consecutive_failure_count, state_signature, "
                    "first_failed_at, last_failed_at, updated_at, "
                    "signature_checked_at, failure_cause) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        golden_alias,
                        1,
                        state_signature,
                        now,
                        now,
                        now,
                        now,
                        failure_cause,
                    ),
                )
                return 1

            new_count = row[0] + 1
            conn.execute(
                "UPDATE fleet_migration_quarantine_state "
                "SET consecutive_failure_count = ?, state_signature = ?, "
                "last_failed_at = ?, updated_at = ?, signature_checked_at = ?, "
                "failure_cause = ? "
                "WHERE golden_alias = ?",
                (
                    new_count,
                    state_signature,
                    now,
                    now,
                    now,
                    failure_cause,
                    golden_alias,
                ),
            )
            return new_count

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def reset_fleet_migration_failure(self, golden_alias: str) -> None:
        """
        Clear any persisted fleet-migration failure/quarantine state for a
        golden repo (Issue #1477) -- called on a successful migration pass,
        or when a quarantine is auto-cleared after detecting a genuine
        on-disk state change since the last recorded failure.
        """

        def operation(conn):
            conn.execute(
                "DELETE FROM fleet_migration_quarantine_state WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def soft_reset_fleet_migration_failure_count(self, golden_alias: str) -> None:
        """
        Issue #1477 Finding N: fallback used by
        `_clear_quarantine_after_detected_repair()` when the full reset
        (DELETE) above fails but a plain UPDATE still works. Zeroes
        `consecutive_failure_count` while KEEPING the row (unlike
        `reset_fleet_migration_failure`, which deletes it) -- this is
        what gives a just-repaired repo a genuinely fresh failure budget
        instead of resuming from a stale, elevated count that
        `record_fleet_migration_failure` would otherwise merely
        increment further.

        A no-op (never raises) when no row exists for `golden_alias` --
        there is nothing to reset, not an error condition (mirrors
        `touch_fleet_migration_failure_check`'s own no-op-on-missing-row
        contract).
        """

        def operation(conn):
            conn.execute(
                "UPDATE fleet_migration_quarantine_state "
                "SET consecutive_failure_count = ? WHERE golden_alias = ?",
                (0, golden_alias),
            )

        self._conn_manager.execute_atomic(operation)

    def touch_fleet_migration_failure_check(self, golden_alias: str) -> None:
        """
        Update ONLY the `signature_checked_at` throttle-bookkeeping
        timestamp for `golden_alias` (Issue #1477 Finding C, Codex round-3
        review) -- used when `is_quarantined()` re-verifies an unchanged
        on-disk signature, so the NEXT recheck window starts fresh WITHOUT
        touching `consecutive_failure_count` or `state_signature` (those
        change ONLY via a genuine new failure or an actual detected
        on-disk change -- never via this bookkeeping-only touch).

        A no-op (never raises) when no row exists for `golden_alias` --
        there is nothing to touch, not an error condition.
        """
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                "UPDATE fleet_migration_quarantine_state "
                "SET signature_checked_at = ? WHERE golden_alias = ?",
                (now, golden_alias),
            )

        self._conn_manager.execute_atomic(operation)

    def get_fleet_migration_failure_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """
        Return the currently persisted fleet-migration failure state for a
        golden repo, or None if it has never failed (or was reset since).
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            "SELECT golden_alias, consecutive_failure_count, state_signature, "
            "first_failed_at, last_failed_at, signature_checked_at, failure_cause "
            "FROM fleet_migration_quarantine_state WHERE golden_alias = ?",
            (golden_alias,),
        ).fetchone()
        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": row[1],
            "state_signature": row[2],
            "first_failed_at": row[3],
            "last_failed_at": row[4],
            "signature_checked_at": row[5],
            "failure_cause": row[6],
        }

    def get_reconcile_breaker_state(self) -> Optional[Dict[str, Any]]:
        """
        Return the current registry-reconcile circuit-breaker state, or
        None if the breaker has never tripped (or was reset since).

        Used by the health-check escalation surface (Bug #1382) so a
        persistently-tripped breaker is visible to admins rather than
        buried in log-only WARNINGs.
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            "SELECT orphan_fingerprint, consecutive_count, first_observed_at, "
            "last_observed_at FROM golden_repo_reconcile_breaker_state "
            "WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "orphan_fingerprint": row[0],
            "consecutive_count": row[1],
            "first_observed_at": row[2],
            "last_observed_at": row[3],
        }

    def record_reconcile_auto_heal_event(self, removed_aliases: List[str]) -> None:
        """
        Persist a discoverable trace of a confirmed registry-reconcile
        auto-removal event (Issue #1383).

        Overwrites the singleton row (id=_RECONCILE_AUTO_HEAL_EVENT_ROW_ID)
        -- only the MOST RECENT auto-heal event needs to be discoverable,
        matching the golden_repo_reconcile_breaker_state convention. This
        record deliberately survives reset_reconcile_breaker_state() and is
        surfaced as the last_golden_repo_reconcile_auto_heal field on the
        /health REST response (HealthCheckResponse), so an operator who
        wasn't watching /health in real time can still discover after the
        fact, through that field, that an automatic mass-removal occurred
        and which repos were affected.

        Args:
            removed_aliases: The aliases actually removed this sweep
                (result.orphans_removed -- never orphans_failed). Must be
                a list of non-empty strings.

        Raises:
            ValueError: If removed_aliases is not a list, or contains a
                non-string / empty element.
        """
        if not isinstance(removed_aliases, list):
            raise ValueError(
                f"removed_aliases must be a list, got: {type(removed_aliases)!r}"
            )
        for alias in removed_aliases:
            if not isinstance(alias, str) or not alias:
                raise ValueError(
                    f"removed_aliases must contain only non-empty strings, "
                    f"got: {alias!r}"
                )

        now = datetime.now(timezone.utc).isoformat()
        removed_aliases_csv = ",".join(removed_aliases)

        def operation(conn):
            conn.execute(
                "INSERT INTO golden_repo_reconcile_auto_heal_event "
                "(id, removed_aliases, occurred_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "removed_aliases = excluded.removed_aliases, "
                "occurred_at = excluded.occurred_at",
                (_RECONCILE_AUTO_HEAL_EVENT_ROW_ID, removed_aliases_csv, now),
            )

        self._conn_manager.execute_atomic(operation)

    def list_fleet_migration_failure_states(self) -> List[Dict[str, Any]]:
        """
        Return every persisted fleet-migration failure-tracking row (Issue
        #1477) -- used by FleetMigrationScheduler.get_stats() to compute a
        dashboard-visible quarantined-repo count without one query per
        golden alias.
        """
        conn = self._conn_manager.get_connection()
        rows = conn.execute(
            "SELECT golden_alias, consecutive_failure_count, state_signature, "
            "first_failed_at, last_failed_at, signature_checked_at, failure_cause "
            "FROM fleet_migration_quarantine_state"
        ).fetchall()
        return [
            {
                "golden_alias": row[0],
                "consecutive_failure_count": row[1],
                "state_signature": row[2],
                "first_failed_at": row[3],
                "last_failed_at": row[4],
                "signature_checked_at": row[5],
                "failure_cause": row[6],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Duplicate-point-id auto-resolution outcome state (Story #1560)
    # ------------------------------------------------------------------

    _DEDUP_STATE_SELECT_COLUMNS = (
        "golden_alias, duplicate_groups, records_before, records_deleted, "
        "winner_kept_groups, whole_group_deleted_groups, collection_total, "
        "first_dropped_at, dropped_at, cleared_at, cleared_reason"
    )

    def record_dedup_outcome(
        self,
        golden_alias: str,
        *,
        duplicate_groups: int,
        records_before: int,
        records_deleted: int,
        winner_kept_groups: int,
        whole_group_deleted_groups: int,
        collection_total: int,
    ) -> Dict[str, Any]:
        """Record one dedup-resolution outcome (AC6/AC7/AC9) -- see
        `_apply_dedup_outcome_upsert` for the cumulative-vs-snapshot
        semantics. `Dict[str, Any]` mirrors this class's own
        `get_fleet_migration_failure_state` return-type convention.
        Returns the resulting row."""
        if not isinstance(golden_alias, str) or not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            _apply_dedup_outcome_upsert(
                conn,
                golden_alias,
                duplicate_groups,
                records_before,
                records_deleted,
                winner_kept_groups,
                whole_group_deleted_groups,
                collection_total,
                now,
            )
            row = conn.execute(
                f"SELECT {self._DEDUP_STATE_SELECT_COLUMNS} "
                f"FROM fleet_migration_dedup_state WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            return _dedup_state_row_to_dict(row)

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def get_dedup_state(self, golden_alias: str) -> Optional[Dict[str, Any]]:
        """Currently persisted dedup-outcome state, or None if absent."""
        if not isinstance(golden_alias, str) or not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            f"SELECT {self._DEDUP_STATE_SELECT_COLUMNS} "
            f"FROM fleet_migration_dedup_state WHERE golden_alias = ?",
            (golden_alias,),
        ).fetchone()
        return None if row is None else _dedup_state_row_to_dict(row)

    def list_dedup_states(self) -> List[Dict[str, Any]]:
        """Every persisted dedup-outcome row -- used by the /health
        surface (AC13-AC18)."""
        conn = self._conn_manager.get_connection()
        rows = conn.execute(
            f"SELECT {self._DEDUP_STATE_SELECT_COLUMNS} "
            f"FROM fleet_migration_dedup_state"
        ).fetchall()
        return [_dedup_state_row_to_dict(row) for row in rows]

    def clear_dedup_state(self, golden_alias: str, reason: str) -> None:
        """Mark a dedup-outcome state as cleared (AC8) -- e.g. after a
        verified successful full re-index. No-op if absent."""
        if not isinstance(golden_alias, str) or not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn):
            conn.execute(
                "UPDATE fleet_migration_dedup_state SET cleared_at = ?, "
                "cleared_reason = ? WHERE golden_alias = ?",
                (now, reason, golden_alias),
            )

        self._conn_manager.execute_atomic(operation)

    def clear_all_dedup_states(self, reason: str) -> int:
        """Story #1589: bulk-clear EVERY currently-active (uncleared)
        dedup-outcome row in one shot -- the Diagnostics tab's "Clear All
        Dedup Warnings" action. Mirrors clear_dedup_state's semantics
        (counts are never erased, only marked cleared) but scoped to
        `WHERE cleared_at IS NULL` instead of a single golden_alias, so an
        already-cleared row is left completely untouched (never
        double-counted). Returns the number of rows actually cleared."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("reason must be a non-empty string")
        now = datetime.now(timezone.utc).isoformat()

        def operation(conn) -> int:
            cursor = conn.execute(
                "UPDATE fleet_migration_dedup_state SET cleared_at = ?, "
                "cleared_reason = ? WHERE cleared_at IS NULL",
                (now, reason),
            )
            return int(cursor.rowcount)

        return self._conn_manager.execute_atomic(operation)

    # ------------------------------------------------------------------
    # Cleanup pending-deletion queue (Bug #1567)
    # ------------------------------------------------------------------

    def schedule_cleanup_deletion(self, index_path: str, scheduled_at: float) -> float:
        """
        Durably record ``index_path`` as pending deletion.

        Idempotent: if a row already exists for ``index_path``, its
        ORIGINAL ``scheduled_at`` is preserved and returned -- a
        re-schedule of an already-queued path must NOT reset its age
        (mirrors the in-process ``setdefault()`` semantics
        CleanupManager relies on: "scheduled_at" means the ORIGINAL
        supersession moment). Otherwise inserts a new row with
        ``scheduled_at`` and returns it unchanged.

        Args:
            index_path: Filesystem path scheduled for deletion.
            scheduled_at: WALL-CLOCK epoch-seconds (``time.time()``) --
                never ``time.monotonic()``, which has no meaning across
                process restarts.

        Returns:
            The authoritative (existing-or-new) scheduled_at.

        Raises:
            ValueError: index_path is not a non-empty string, or
                scheduled_at is not a finite number.
        """
        if not isinstance(index_path, str) or not index_path.strip():
            raise ValueError(
                f"index_path must be a non-empty string, got {index_path!r}"
            )
        if not isinstance(scheduled_at, (int, float)) or isinstance(scheduled_at, bool):
            raise ValueError(
                f"scheduled_at must be a real number, got {scheduled_at!r}"
            )
        scheduled_at = float(scheduled_at)
        if not math.isfinite(scheduled_at):
            raise ValueError(
                f"scheduled_at must be a finite number, got {scheduled_at!r}"
            )

        def operation(conn):
            row = conn.execute(
                "SELECT scheduled_at FROM cleanup_pending_deletion_state "
                "WHERE index_path = ?",
                (index_path,),
            ).fetchone()
            if row is not None:
                return float(row[0])
            conn.execute(
                "INSERT INTO cleanup_pending_deletion_state "
                "(index_path, scheduled_at) VALUES (?, ?)",
                (index_path, scheduled_at),
            )
            return float(scheduled_at)

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def list_cleanup_pending_deletions(self) -> List[Dict[str, Any]]:
        """
        Return every durably-pending deletion row.

        Used to hydrate a freshly-constructed CleanupManager's in-memory
        queue -- recovering exactly what a PRIOR process/worker already
        scheduled, closing the silent-loss window a bare in-process queue
        left on every restart/worker-recycle (Bug #1567). Wrapped through
        the same execute_atomic() transaction boundary every write in
        this class uses, so a concurrent writer cannot be observed
        mid-transaction.
        """

        def operation(conn):
            rows = conn.execute(
                "SELECT index_path, scheduled_at FROM cleanup_pending_deletion_state"
            ).fetchall()
            return [
                {"index_path": row[0], "scheduled_at": float(row[1])} for row in rows
            ]

        return self._conn_manager.execute_atomic(operation)  # type: ignore[no-any-return]

    def remove_cleanup_pending_deletion(self, index_path: str) -> None:
        """Remove one durably-pending deletion row. Idempotent -- a no-op
        when no row exists for ``index_path`` (Bug #1567).

        Raises:
            ValueError: index_path is not a non-empty string.
        """
        if not isinstance(index_path, str) or not index_path.strip():
            raise ValueError(
                f"index_path must be a non-empty string, got {index_path!r}"
            )

        def operation(conn):
            conn.execute(
                "DELETE FROM cleanup_pending_deletion_state WHERE index_path = ?",
                (index_path,),
            )

        self._conn_manager.execute_atomic(operation)

    def close(self) -> None:
        """Close database connections."""
        self._conn_manager.close_all()


def _insert_new_dedup_row(
    conn: Any,
    golden_alias: str,
    duplicate_groups: int,
    records_before: int,
    records_deleted: int,
    winner_kept_groups: int,
    whole_group_deleted_groups: int,
    collection_total: int,
    now: str,
) -> None:
    """Story #1560 AC7: first-ever dedup outcome for `golden_alias`."""
    conn.execute(
        "INSERT INTO fleet_migration_dedup_state "
        "(golden_alias, duplicate_groups, records_before, "
        "records_deleted, winner_kept_groups, "
        "whole_group_deleted_groups, collection_total, "
        "first_dropped_at, dropped_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            golden_alias,
            duplicate_groups,
            records_before,
            records_deleted,
            winner_kept_groups,
            whole_group_deleted_groups,
            collection_total,
            now,
            now,
        ),
    )


def _update_existing_dedup_row(
    conn: Any,
    golden_alias: str,
    existing_counts: tuple,
    duplicate_groups: int,
    records_before: int,
    records_deleted: int,
    winner_kept_groups: int,
    whole_group_deleted_groups: int,
    collection_total: int,
    now: str,
) -> None:
    """Story #1560 AC9: cumulative UPDATE. `existing_counts` is the prior
    (duplicate_groups, records_deleted, winner_kept_groups,
    whole_group_deleted_groups) tuple -- ADDED to, since these permanent
    deletions must never be double-counted or discarded. `records_before`/
    `collection_total` are OVERWRITTEN (snapshot semantics -- summing a
    collection's size across passes would be meaningless). Resets
    `cleared_at`/`cleared_reason` -- a fresh outcome is active again."""
    conn.execute(
        "UPDATE fleet_migration_dedup_state SET duplicate_groups = ?, "
        "records_before = ?, records_deleted = ?, winner_kept_groups = ?, "
        "whole_group_deleted_groups = ?, collection_total = ?, "
        "dropped_at = ?, cleared_at = NULL, cleared_reason = NULL "
        "WHERE golden_alias = ?",
        (
            existing_counts[0] + duplicate_groups,
            records_before,
            existing_counts[1] + records_deleted,
            existing_counts[2] + winner_kept_groups,
            existing_counts[3] + whole_group_deleted_groups,
            collection_total,
            now,
            golden_alias,
        ),
    )


def _apply_dedup_outcome_upsert(
    conn: Any,
    golden_alias: str,
    duplicate_groups: int,
    records_before: int,
    records_deleted: int,
    winner_kept_groups: int,
    whole_group_deleted_groups: int,
    collection_total: int,
    now: str,
) -> None:
    """Story #1560 AC6/AC7/AC9: dispatch to insert or cumulative-update
    for one `fleet_migration_dedup_state` row."""
    existing = conn.execute(
        "SELECT duplicate_groups, records_deleted, winner_kept_groups, "
        "whole_group_deleted_groups FROM fleet_migration_dedup_state "
        "WHERE golden_alias = ?",
        (golden_alias,),
    ).fetchone()
    if existing is None:
        _insert_new_dedup_row(
            conn,
            golden_alias,
            duplicate_groups,
            records_before,
            records_deleted,
            winner_kept_groups,
            whole_group_deleted_groups,
            collection_total,
            now,
        )
    else:
        _update_existing_dedup_row(
            conn,
            golden_alias,
            existing,
            duplicate_groups,
            records_before,
            records_deleted,
            winner_kept_groups,
            whole_group_deleted_groups,
            collection_total,
            now,
        )
