"""
PostgreSQL backend for API metrics storage (Story #502).

Drop-in replacement for ApiMetricsSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the ApiMetricsBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required.

Unlike error-path backends, metric insert failures are caught and logged as
warnings rather than propagated -- a failed metric write must never crash
the application.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class ApiMetricsPostgresBackend:
    """
    PostgreSQL backend for API metrics storage.

    Satisfies the ApiMetricsBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).

    Insert failures are swallowed with a warning so that metric writes never
    bring down the application.
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
        """Create the api_metrics table and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_metrics (
                        id SERIAL PRIMARY KEY,
                        metric_type TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        node_id TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_api_metrics_pg_type_timestamp
                    ON api_metrics(metric_type, timestamp)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_api_metrics_pg_node_id
                    ON api_metrics(node_id)
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: schema setup failed: %s", exc)

    def insert_metric(
        self,
        metric_type: str,
        timestamp: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single metric record.

        Failures are caught and logged as warnings to prevent metric writes from
        crashing the application.

        Args:
            metric_type: Category ('semantic', 'other_index', 'regex', 'other_api').
            timestamp: ISO 8601 timestamp. Uses current UTC time when None.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        now = (
            timestamp
            if timestamp is not None
            else datetime.now(timezone.utc).isoformat()
        )
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO api_metrics (metric_type, timestamp, node_id)
                    VALUES (%s, %s, %s)
                    """,
                    (metric_type, now, node_id),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: insert_metric failed: %s", exc)

    def get_metrics(
        self,
        window_seconds: int = 3600,
        node_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return metric counts within the rolling window.

        Args:
            window_seconds: Time window in seconds (default 3600 = 1 hour).
            node_id: When provided, filter to metrics from this node only.
                     When None, aggregate across all nodes.

        Returns:
            Dict with keys: semantic_searches, other_index_searches,
            regex_searches, other_api_calls.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        ).isoformat()

        try:
            with self._pool.connection() as conn:
                if node_id is not None:
                    rows = conn.execute(
                        """
                        SELECT metric_type, COUNT(*) as count
                        FROM api_metrics
                        WHERE timestamp >= %s AND node_id = %s
                        GROUP BY metric_type
                        """,
                        (cutoff, node_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT metric_type, COUNT(*) as count
                        FROM api_metrics
                        WHERE timestamp >= %s
                        GROUP BY metric_type
                        """,
                        (cutoff,),
                    ).fetchall()
        except Exception as exc:
            logger.warning("ApiMetricsPostgresBackend: get_metrics failed: %s", exc)
            rows = []

        counts = {row[0]: row[1] for row in rows}
        return {
            "semantic_searches": counts.get("semantic", 0),
            "other_index_searches": counts.get("other_index", 0),
            "regex_searches": counts.get("regex", 0),
            "other_api_calls": counts.get("other_api", 0),
        }

    def cleanup_old(self, max_age_seconds: int = 86400) -> int:
        """Delete metric records older than max_age_seconds.

        Args:
            max_age_seconds: Records older than this many seconds are deleted
                             (default 86400 = 24 hours).

        Returns:
            Number of rows deleted.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()

        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM api_metrics WHERE timestamp < %s",
                (cutoff,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()

        if deleted:
            logger.debug(
                "ApiMetricsPostgresBackend: cleaned up %d old metric records", deleted
            )
        return deleted

    def reset(self) -> None:
        """Delete all metric records (used for testing / manual resets)."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM api_metrics")
            conn.commit()

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
