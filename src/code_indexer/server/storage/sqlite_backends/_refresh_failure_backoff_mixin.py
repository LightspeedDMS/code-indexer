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


def create_refresh_failure_backoff_table(conn: sqlite3.Connection) -> None:
    """Bug #2022: per-golden-alias refresh failure backoff state.
    ``last_failed_at`` is wall-clock epoch seconds (``time.time()``) so the
    backoff window survives restarts. ``pending_trigger`` marks a system
    refresh that was deferred and must fire once the backoff ends.
    Idempotent; also adds the column to a table created before it existed."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_failure_backoff_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_detail TEXT,
            last_failed_at REAL NOT NULL,
            updated_at TEXT,
            pending_trigger INTEGER NOT NULL DEFAULT 0
        )
    """
    )
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(refresh_failure_backoff_state)")
    }
    if "pending_trigger" not in columns:
        conn.execute(
            "ALTER TABLE refresh_failure_backoff_state "
            "ADD COLUMN pending_trigger INTEGER NOT NULL DEFAULT 0"
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
    "pending_trigger"
)


def _state_from_row(row: Any) -> Dict[str, Any]:
    return {
        "golden_alias": row[0],
        "consecutive_failure_count": int(row[1]),
        "last_detail": row[2],
        "last_failed_at": float(row[3]),
        "pending_trigger": bool(row[4]),
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

    def reset_refresh_failure_backoff(self, golden_alias: str) -> None:
        """Clear the backoff state (a verified refresh succeeded). A no-op
        for an alias with no recorded state."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM refresh_failure_backoff_state WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def mark_refresh_trigger_pending(self, golden_alias: str) -> None:
        """Remember a system refresh trigger deferred by the backoff, so it
        fires once the backoff ends. A no-op when no backoff is recorded."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE refresh_failure_backoff_state SET pending_trigger = 1 "
                "WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)

    def list_pending_refresh_triggers(self) -> List[Dict[str, Any]]:
        """Backoff states that carry a deferred trigger (only failing
        aliases, never the whole fleet)."""
        rows = (
            self._conn_manager.get_connection()
            .execute(
                f"SELECT {_STATE_COLUMNS} FROM refresh_failure_backoff_state "
                "WHERE pending_trigger = 1 ORDER BY golden_alias"
            )
            .fetchall()
        )
        return [_state_from_row(row) for row in rows]

    def claim_pending_refresh_trigger(self, golden_alias: str) -> bool:
        """Atomically take the deferred trigger; True for exactly one caller."""
        if not golden_alias:
            raise ValueError("golden_alias must be a non-empty string")

        def operation(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "UPDATE refresh_failure_backoff_state SET pending_trigger = 0 "
                "WHERE golden_alias = ? AND pending_trigger = 1",
                (golden_alias,),
            )
            return cursor.rowcount == 1

        return bool(self._conn_manager.execute_atomic(operation))
