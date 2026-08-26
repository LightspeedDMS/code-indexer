"""
PostgreSQL backend for diagnostics results storage (Story #525).

Drop-in replacement for DiagnosticsSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the DiagnosticsBackend Protocol
(protocols.py).

Schema (diagnostic_results) is owned entirely by the SQL migrations
(storage/postgres/migrations/sql/) -- this backend does NOT create or
alter any table. `service_init.py` always runs `MigrationRunner` before
`StorageFactory.create_backends()` constructs this class, so schema is
guaranteed present by the time any instance exists (Bug #1662, mirroring
Bug #1655's F4 remediation for wiki_cache_backend.py: a previous self-heal
`CREATE TABLE IF NOT EXISTS` here was dead code in every real deployment
and had silently drifted out of sync with the real migration's column
types -- `results_json`/`run_at` declared TEXT/TEXT here vs. the
migration's JSONB/TIMESTAMPTZ -- removed rather than re-synced so there
is no second copy of the schema left to drift again).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .connection_pool import ConnectionPool


class DiagnosticsPostgresBackend:
    """
    PostgreSQL backend for diagnostics results storage.

    Satisfies the DiagnosticsBackend Protocol (protocols.py).
    All mutations commit immediately after DML execution.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Schema is assumed to already exist (see module docstring) -- this
        constructor does not touch the database.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

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

    def load_all_results(self) -> List[Tuple[str, object, object]]:
        """Return all rows as list of (category, results_json, run_at) tuples.

        Bug #1662: `results_json`/`run_at` are honestly typed `object`, not
        `str`. `results_json` is JSONB -- psycopg deserializes it to a
        native `dict`/`list` before the row reaches application code.
        `run_at` is TIMESTAMPTZ -- psycopg deserializes it to a native
        (often tz-aware) `datetime`. Neither is ever a `str` on this
        backend; a caller MUST normalize via `parse_json_column()` /
        an equivalent datetime coercion helper (see
        `diagnostics_service.py`'s `_coerce_run_at()`).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT category, results_json, run_at FROM diagnostic_results"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def load_category_results(self, category: str) -> Optional[Tuple[object, object]]:
        """Return (results_json, run_at) for a category, or None if absent.

        Same dual-shape (JSONB dict/list, TIMESTAMPTZ datetime) contract as
        `load_all_results()` -- see its docstring.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT results_json, run_at FROM diagnostic_results WHERE category = %s",
                (category,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
