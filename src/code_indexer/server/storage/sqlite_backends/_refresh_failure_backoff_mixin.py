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
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..database_manager import DatabaseConnectionManager


def create_refresh_failure_backoff_table(conn: sqlite3.Connection) -> None:
    """Bug #2022: per-golden-alias refresh failure backoff state.
    ``last_failed_at`` is wall-clock epoch seconds (``time.time()``) so the
    backoff window survives restarts."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_failure_backoff_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
            last_detail TEXT,
            last_failed_at REAL NOT NULL,
            updated_at TEXT
        )
    """
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
            row = conn.execute(
                "SELECT consecutive_failure_count FROM refresh_failure_backoff_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            count = 1 if row is None else int(row[0]) + 1
            conn.execute(
                "INSERT OR REPLACE INTO refresh_failure_backoff_state "
                "(golden_alias, consecutive_failure_count, last_detail, "
                "last_failed_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (golden_alias, count, detail, failed_at, updated_at),
            )
            return count

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
                "SELECT golden_alias, consecutive_failure_count, last_detail, "
                "last_failed_at FROM refresh_failure_backoff_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return {
            "golden_alias": row[0],
            "consecutive_failure_count": int(row[1]),
            "last_detail": row[2],
            "last_failed_at": float(row[3]),
        }

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
