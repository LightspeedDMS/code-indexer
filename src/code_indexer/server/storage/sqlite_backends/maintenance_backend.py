"""
SQLite backend for maintenance mode state storage (Story #529).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import Any, Dict

from ..database_manager import DatabaseConnectionManager


class MaintenanceSqliteBackend:
    """
    SQLite backend for maintenance mode state storage (Story #529).

    Satisfies the MaintenanceBackend Protocol.
    Uses the main cidx_server.db with a single-row maintenance_state table.

    In standalone (SQLite) mode the MaintenanceService reads this table so
    maintenance state survives server restarts. In cluster (PostgreSQL) mode
    the MaintenancePostgresBackend provides cross-node coordination.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to SQLite database file (cidx_server.db).
        """
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create maintenance_state table if it does not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    started_at TEXT,
                    started_by TEXT
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def enter_maintenance(self, started_by: str, reason: str, started_at: str) -> None:
        """Persist maintenance mode as active (upsert single row).

        Args:
            started_by: Username or identifier of who activated maintenance mode.
            reason: Human-readable reason for entering maintenance mode.
            started_at: ISO 8601 timestamp when maintenance mode was activated.
        """

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT OR REPLACE INTO maintenance_state
                    (id, enabled, reason, started_at, started_by)
                VALUES (1, 1, ?, ?, ?)
                """,
                (reason, started_at, started_by),
            )

        self._conn_manager.execute_atomic(_op)

    def exit_maintenance(self) -> None:
        """Mark maintenance mode as inactive."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE maintenance_state SET enabled = 0 WHERE id = 1")

        self._conn_manager.execute_atomic(_op)

    def get_status(self) -> Dict[str, Any]:
        """Return current maintenance state dict.

        Returns:
            Dict with keys: enabled (bool), reason, started_at, started_by.
            enabled is False when no row exists or row has enabled=0.
        """
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT enabled, reason, started_at, started_by FROM maintenance_state WHERE id = 1"
        )
        row = cursor.fetchone()
        if row is None:
            return {
                "enabled": False,
                "reason": None,
                "started_at": None,
                "started_by": None,
            }
        return {
            "enabled": bool(row[0]),
            "reason": row[1],
            "started_at": row[2],
            "started_by": row[3],
        }

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
