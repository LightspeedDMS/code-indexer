"""
SQLite backend for diagnostics results storage (Story #525).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import sqlite3
from typing import List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager


class DiagnosticsSqliteBackend:
    """
    SQLite backend for diagnostics results storage (Story #525).

    Satisfies the DiagnosticsBackend Protocol.
    Uses the main cidx_server.db (diagnostic_results table).
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
        """Create diagnostic_results table if it does not already exist."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_results (
                    category TEXT PRIMARY KEY,
                    results_json TEXT NOT NULL,
                    run_at TEXT NOT NULL
                )
                """
            )

        self._conn_manager.execute_atomic(_op)

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        """Persist (upsert) diagnostic results for a category."""

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO diagnostic_results (category, results_json, run_at) VALUES (?, ?, ?)",
                (category, results_json, run_at),
            )

        self._conn_manager.execute_atomic(_op)

    def load_all_results(self) -> List[Tuple[str, str, str]]:
        """Return all rows as list of (category, results_json, run_at) tuples."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT category, results_json, run_at FROM diagnostic_results"
        )
        return [(row[0], row[1], row[2]) for row in cursor.fetchall()]

    def load_category_results(self, category: str) -> Optional[Tuple[str, str]]:
        """Return (results_json, run_at) for a category, or None if absent."""
        conn = self._conn_manager.get_connection()
        cursor = conn.execute(
            "SELECT results_json, run_at FROM diagnostic_results WHERE category = ?",
            (category,),
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        """Close the DatabaseConnectionManager connection."""
        self._conn_manager.close_all()
