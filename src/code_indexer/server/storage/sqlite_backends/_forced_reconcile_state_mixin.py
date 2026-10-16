"""Per-alias forced-reconcile state for GoldenRepoMetadataSqliteBackend.

The refresh scheduler forces a reconcile when a repository's index metadata
shows a stale signal (interrupted run, drifted commit) although git reports
no new commits. If the forced reconcile completes and the SAME signal is
still there, forcing again changes nothing. The signal text and the number
of consecutive forced reconciles that left it unchanged are persisted here
-- never in per-node memory -- so the bound survives restarts and is seen
by every node. Split into its own module because the backend's main module
is near the project's 1,000-line-per-file limit.
"""

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from ..database_manager import DatabaseConnectionManager


def create_forced_reconcile_state_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL, shared by ensure_table_exists and DatabaseSchema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forced_reconcile_state (
            golden_alias TEXT PRIMARY KEY NOT NULL,
            signal TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            updated_at TEXT
        )
    """
    )


def delete_forced_reconcile_state_for_repo(
    conn: sqlite3.Connection, alias: str
) -> None:
    """Drop a removed golden repo's state inside the caller's transaction,
    under both its bare and its ``-global`` alias."""
    conn.execute(
        "DELETE FROM forced_reconcile_state WHERE golden_alias IN (?, ?)",
        (alias, f"{alias}-global"),
    )


def _require(value: str, name: str) -> None:
    if not value:
        raise ValueError(f"{name} must be a non-empty string")


class _ForcedReconcileStateSqliteMixin:
    """Forced-reconcile state methods (see module docstring)."""

    # Supplied at runtime by GoldenRepoMetadataSqliteBackend.__init__.
    _conn_manager: "DatabaseConnectionManager"

    def record_forced_reconcile(self, golden_alias: str, signal: str) -> int:
        """Record one forced reconcile for ``signal``; return the number of
        consecutive forced reconciles for this same signal (1 when the
        stored signal differs or none is stored). One atomic upsert."""
        _require(golden_alias, "golden_alias")
        _require(signal, "signal")
        updated_at = datetime.now(timezone.utc).isoformat()

        def operation(conn: sqlite3.Connection) -> int:
            conn.execute(
                "INSERT INTO forced_reconcile_state "
                "(golden_alias, signal, attempt_count, updated_at) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(golden_alias) DO UPDATE SET "
                "attempt_count = CASE WHEN forced_reconcile_state.signal = "
                "excluded.signal THEN forced_reconcile_state.attempt_count + 1 "
                "ELSE 1 END, "
                "signal = excluded.signal, updated_at = excluded.updated_at",
                (golden_alias, signal, updated_at),
            )
            row = conn.execute(
                "SELECT attempt_count FROM forced_reconcile_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            ).fetchone()
            return int(row[0])

        return int(self._conn_manager.execute_atomic(operation))

    def get_forced_reconcile_state(self, golden_alias: str) -> Optional[Dict[str, Any]]:
        """Return ``{"signal", "attempt_count"}`` or None when none is stored."""
        _require(golden_alias, "golden_alias")
        row = (
            self._conn_manager.get_connection()
            .execute(
                "SELECT signal, attempt_count FROM forced_reconcile_state "
                "WHERE golden_alias = ?",
                (golden_alias,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return {"signal": row[0], "attempt_count": int(row[1])}

    def clear_forced_reconcile_state(self, golden_alias: str) -> None:
        """Forget the state (the stale signal cleared)."""
        _require(golden_alias, "golden_alias")

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM forced_reconcile_state WHERE golden_alias = ?",
                (golden_alias,),
            )

        self._conn_manager.execute_atomic(operation)
