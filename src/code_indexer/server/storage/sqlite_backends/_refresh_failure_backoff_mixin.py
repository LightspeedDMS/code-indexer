"""Per-alias refresh failure backoff state for GoldenRepoMetadataSqliteBackend
(Bug #2022 Gap 4).

A refresh that keeps failing for a reason the self-heal cannot repair (disk
full, permission denied, an unrepairable corrupt store) must not be
re-submitted every cycle. The consecutive-failure count and the wall-clock
time of the last failure are persisted here -- never in per-node memory --
so every submission path on every node sees the same backoff.

Split into its own module because the backend's main module and its other
mixin are already near the project's 1,000-line-per-file limit.
"""

import sqlite3
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ..database_manager import DatabaseConnectionManager


#: Columns added after the table first shipped; added in place when missing.
_ADDED_COLUMNS = (
    ("pending_trigger", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_due_at", "REAL"),
    ("pending_marked_at", "REAL"),
)


def create_refresh_failure_backoff_table(conn: sqlite3.Connection) -> None:
    """Bug #2022: per-golden-alias refresh failure backoff state.
    ``last_failed_at`` is wall-clock epoch seconds (``time.time()``) so the
    backoff window survives restarts. ``pending_trigger`` marks a deferred
    system refresh, due at ``pending_due_at``; ``pending_marked_at`` is when
    it was last deferred. Idempotent; upgrades a table created earlier."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_failure_backoff_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_detail TEXT,
            last_failed_at REAL NOT NULL,
            updated_at TEXT,
            pending_trigger INTEGER NOT NULL DEFAULT 0,
            pending_due_at REAL,
            pending_marked_at REAL
        )
    """
    )
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(refresh_failure_backoff_state)")
    }
    for name, definition in _ADDED_COLUMNS:
        if name not in columns:
            conn.execute(
                f"ALTER TABLE refresh_failure_backoff_state "
                f"ADD COLUMN {name} {definition}"
            )
    # A trigger recorded before pending_due_at existed is due at once.
    conn.execute(
        "UPDATE refresh_failure_backoff_state SET pending_due_at = last_failed_at "
        "WHERE pending_trigger = 1 AND pending_due_at IS NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_refresh_failure_backoff_due "
        "ON refresh_failure_backoff_state (pending_trigger, pending_due_at)"
    )


def delete_refresh_failure_backoff_for_repo(
    conn: sqlite3.Connection, alias: str
) -> None:
    """Bug #2022: drop the backoff of a removed golden repo inside the
    caller's transaction, under both its bare and its ``-global`` alias, so
    a repo later registered under the same name starts clean."""
    conn.execute(
        "DELETE FROM refresh_failure_backoff_state WHERE golden_alias IN (?, ?)",
        (alias, f"{alias}-global"),
    )


_STATE_COLUMNS = (
    "golden_alias, consecutive_failure_count, last_detail, last_failed_at, "
    "pending_trigger, pending_due_at, pending_marked_at"
)

#: Deferred triggers that are due (index idx_refresh_failure_backoff_due).
DUE_TRIGGERS_SQL = (
    f"SELECT {_STATE_COLUMNS} FROM refresh_failure_backoff_state "
    "WHERE pending_trigger = 1 AND pending_due_at <= ? ORDER BY pending_due_at"
)


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _state_from_row(row: Any) -> Dict[str, Any]:
    return {
        "golden_alias": row[0],
        "consecutive_failure_count": int(row[1]),
        "last_detail": row[2],
        "last_failed_at": float(row[3]),
        "pending_trigger": bool(row[4]),
        "pending_due_at": _optional_float(row[5]),
        "pending_marked_at": _optional_float(row[6]),
    }


class _RefreshFailureBackoffSqliteMixin:
    """Refresh failure backoff methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataSqliteBackend.__init__.
    _conn_manager: "DatabaseConnectionManager"

    def record_refresh_failure_backoff(self, golden_alias: str, detail: str) -> int:
        """Record one non-repairable refresh failure; return the
        consecutive-failure count after recording it."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if not detail:
            raise ValueError("detail must be a non-empty string")
        failed_at = time.time()
        updated_at = datetime.now(timezone.utc).isoformat()

        def operation(conn: sqlite3.Connection) -> int:
            # UPSERT: a pending deferred trigger survives further failures.
            conn.execute(
                "INSERT INTO refresh_failure_backoff_state "
                "(golden_alias, consecutive_failure_count, last_detail, "
                "last_failed_at, updated_at) VALUES (?, 1, ?, ?, ?) "
                "ON CONFLICT(golden_alias) DO UPDATE SET "
                "consecutive_failure_count = consecutive_failure_count + 1, "
                "last_detail = excluded.last_detail, "
                "last_failed_at = excluded.last_failed_at, "
                "updated_at = excluded.updated_at",
                (golden_alias, detail, failed_at, updated_at),
            )
            row = conn.execute(
                "SELECT consecutive_failure_count FROM refresh_failure_backoff_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            return int(row[0])

        return int(self._conn_manager.execute_atomic(operation))

    def get_refresh_failure_backoff_state(
        self, golden_alias: str
    ) -> Optional[Dict[str, Any]]:
        """Return the persisted backoff state, or None if none is recorded."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        row = (
            self._conn_manager.get_connection()
            .execute(
                f"SELECT {_STATE_COLUMNS} FROM refresh_failure_backoff_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            )
            .fetchone()
        )
        return None if row is None else _state_from_row(row)

    def mark_refresh_trigger_pending(self, golden_alias: str, due_at: float) -> bool:
        """Remember a system refresh trigger deferred by the backoff, due at
        *due_at*. False when no backoff row exists any more (it was resolved
        concurrently), so nothing defers the trigger."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        marked_at = time.time()

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE refresh_failure_backoff_state SET pending_trigger = 1, "
                "pending_due_at = ?, pending_marked_at = ? WHERE golden_alias = ?",
                (due_at, marked_at, golden_alias),
            )
            return cursor.rowcount == 1

        return bool(self._conn_manager.execute_atomic(operation))

    def list_due_refresh_triggers(self, now: float) -> List[Dict[str, Any]]:
        """Deferred triggers due at *now* (index-backed; only failing aliases
        carry one, never the whole fleet)."""
        rows = (
            self._conn_manager.get_connection()
            .execute(DUE_TRIGGERS_SQL, (now,))
            .fetchall()
        )
        return [_state_from_row(row) for row in rows]

    def lease_pending_refresh_trigger(
        self, golden_alias: str, now: float, until: float
    ) -> bool:
        """Atomically take a due trigger until *until*: True for exactly one
        caller. The trigger stays pending, so a crash before the refresh
        completes only delays it to the lease end."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        if until <= now:
            raise ValueError("lease end must be later than now")

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE refresh_failure_backoff_state SET pending_due_at = ? "
                "WHERE golden_alias = ? AND pending_trigger = 1 "
                "AND pending_due_at <= ?",
                (until, golden_alias, now),
            )
            return cursor.rowcount == 1

        return bool(self._conn_manager.execute_atomic(operation))

    def resolve_refresh_failure_backoff(
        self, golden_alias: str, cycle_started_at: float
    ) -> None:
        """A verified publish resolves the failure and any trigger it covers.
        A trigger deferred after *cycle_started_at* may not be covered: its
        row survives re-armed (no failure left, due now)."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")
        now = time.time()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM refresh_failure_backoff_state WHERE golden_alias = ? "
                "AND NOT (pending_trigger = 1 "
                "AND COALESCE(pending_marked_at, 0) >= ?)",
                (golden_alias, cycle_started_at),
            )
            conn.execute(
                "UPDATE refresh_failure_backoff_state SET "
                "consecutive_failure_count = 0, pending_due_at = ?, updated_at = ? "
                "WHERE golden_alias = ?",
                (now, datetime.now(timezone.utc).isoformat(), golden_alias),
            )

        self._conn_manager.execute_atomic(operation)
