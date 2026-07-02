"""
PostgreSQL backend for maintenance mode state storage (Story #529).

Drop-in replacement for MaintenanceSqliteBackend using psycopg v3 sync
connections via ConnectionPool.  Satisfies the MaintenanceBackend Protocol
(protocols.py).

Provides cluster-wide coordination: all nodes read from the same
maintenance_state row, so entering/exiting maintenance on one node
immediately affects all nodes in the cluster.

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class MaintenancePostgresBackend:
    """
    PostgreSQL backend for maintenance mode state storage.

    Satisfies the MaintenanceBackend Protocol (protocols.py).
    Stores a single row (id=1) in maintenance_state table.
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
        """Create the maintenance_state table if it does not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS maintenance_state (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        reason TEXT,
                        started_at TEXT,
                        started_by TEXT
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("MaintenancePostgresBackend: schema setup failed: %s", exc)

    def enter_maintenance(self, started_by: str, reason: str, started_at: str) -> None:
        """Persist maintenance mode as active (upsert single row).

        Args:
            started_by: Username or identifier of who activated maintenance mode.
            reason: Human-readable reason for entering maintenance mode.
            started_at: ISO 8601 timestamp when maintenance mode was activated.
        """
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO maintenance_state (id, enabled, reason, started_at, started_by)
                VALUES (1, TRUE, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    reason = EXCLUDED.reason,
                    started_at = EXCLUDED.started_at,
                    started_by = EXCLUDED.started_by
                """,
                (reason, started_at, started_by),
            )
            conn.commit()

    def exit_maintenance(self) -> None:
        """Mark maintenance mode as inactive."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE maintenance_state SET enabled = FALSE WHERE id = 1")
            conn.commit()

    def get_status(self) -> Dict[str, Any]:
        """Return current maintenance state dict.

        Returns:
            Dict with keys: enabled (bool), reason, started_at, started_by.
            enabled is False when no row exists or row has enabled=False.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT enabled, reason, started_at, started_by "
                "FROM maintenance_state WHERE id = 1"
            ).fetchone()

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
        """No-op: pool lifecycle is managed externally."""
