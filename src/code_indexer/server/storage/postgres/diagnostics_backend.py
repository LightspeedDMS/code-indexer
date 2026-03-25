"""
PostgreSQL backend for diagnostics results storage (Story #525).

Drop-in replacement for DiagnosticsSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the DiagnosticsBackend Protocol
(protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class DiagnosticsPostgresBackend:
    """
    PostgreSQL backend for diagnostics results storage.

    Satisfies the DiagnosticsBackend Protocol (protocols.py).
    All mutations commit immediately after DML execution.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool and ensure the table exists.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the diagnostic_results table if it does not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS diagnostic_results (
                        category TEXT PRIMARY KEY,
                        results_json TEXT NOT NULL,
                        run_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("DiagnosticsPostgresBackend: schema setup failed: %s", exc)

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        """Persist (upsert) diagnostic results for a category."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO diagnostic_results (category, results_json, run_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (category) DO UPDATE SET
                    results_json = EXCLUDED.results_json,
                    run_at = EXCLUDED.run_at
                """,
                (category, results_json, run_at),
            )
            conn.commit()

    def load_all_results(self) -> List[Tuple[str, str, str]]:
        """Return all rows as list of (category, results_json, run_at) tuples."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT category, results_json, run_at FROM diagnostic_results"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def load_category_results(self, category: str) -> Optional[Tuple[str, str]]:
        """Return (results_json, run_at) for a category, or None if absent."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT results_json, run_at FROM diagnostic_results WHERE category = %s",
                (category,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
