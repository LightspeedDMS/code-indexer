"""
PostgreSQL backend for operational log storage (Story #501).

Drop-in replacement for LogsSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the LogsBackend Protocol (protocols.py).

Table created on first use (CREATE TABLE IF NOT EXISTS) so no separate
migration step is required for the logs table.

Unlike the other PostgreSQL backends, log insert failures are caught and
logged as warnings rather than propagated -- a failed log write must never
crash the application.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)


class LogsPostgresBackend:
    """
    PostgreSQL backend for operational log storage.

    Satisfies the LogsBackend Protocol (protocols.py).
    All mutations commit immediately after executing the DML statement.
    Read operations do not commit (auto-commit is fine for SELECT).

    Insert failures are swallowed with a warning so that logging never
    brings down the application.
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
        """Create the logs table and indexes if they do not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        level TEXT NOT NULL,
                        source TEXT,
                        message TEXT,
                        correlation_id TEXT,
                        user_id TEXT,
                        request_path TEXT,
                        extra_data TEXT,
                        node_id TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_pg_timestamp ON logs(timestamp)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_pg_level ON logs(level)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_pg_node_id ON logs(node_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_logs_pg_correlation_id ON logs(correlation_id)"
                )
                conn.commit()
        except Exception as exc:
            logger.warning("LogsPostgresBackend: schema setup failed: %s", exc)

    def insert_log(
        self,
        timestamp: str,
        level: str,
        source: Optional[str] = None,
        message: Optional[str] = None,
        correlation_id: Optional[str] = None,
        user_id: Optional[str] = None,
        request_path: Optional[str] = None,
        extra_data: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        """Insert a single log record.

        Failures are caught and logged as warnings to prevent log writes from
        crashing the application.

        Args:
            timestamp: ISO 8601 timestamp string.
            level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
            source: Logger name / source identifier.
            message: Formatted log message text.
            correlation_id: Optional request correlation ID.
            user_id: Optional user identifier.
            request_path: Optional HTTP request path.
            extra_data: Optional JSON-serialised extra fields.
            node_id: Optional cluster node identifier (NULL in standalone).
        """
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO logs
                        (timestamp, level, source, message, correlation_id,
                         user_id, request_path, extra_data, node_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        timestamp,
                        level,
                        source,
                        message,
                        correlation_id,
                        user_id,
                        request_path,
                        extra_data,
                        node_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("LogsPostgresBackend: insert_log failed: %s", exc)

    def _build_query_conditions(
        self,
        level: Optional[str],
        source: Optional[str],
        correlation_id: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
        node_id: Optional[str],
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and params list for log queries (parameterized)."""
        conditions: List[str] = []
        params: List[Any] = []
        if level is not None:
            conditions.append("level = %s")
            params.append(level)
        if source is not None:
            conditions.append("source = %s")
            params.append(source)
        if correlation_id is not None:
            conditions.append("correlation_id = %s")
            params.append(correlation_id)
        if date_from is not None:
            conditions.append("timestamp >= %s")
            params.append(date_from)
        if date_to is not None:
            conditions.append("timestamp <= %s")
            params.append(date_to)
        if node_id is not None:
            conditions.append("node_id = %s")
            params.append(node_id)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where_clause, params

    def _row_to_log_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a log record dict."""
        # created_at may come back as a datetime from PostgreSQL
        created_at = row[10]
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()

        return {
            "id": row[0],
            "timestamp": row[1],
            "level": row[2],
            "source": row[3],
            "message": row[4],
            "correlation_id": row[5],
            "user_id": row[6],
            "request_path": row[7],
            "extra_data": row[8],
            "node_id": row[9],
            "created_at": created_at,
        }

    def query_logs(
        self,
        level: Optional[str] = None,
        source: Optional[str] = None,
        correlation_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        node_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query log records with optional filtering and pagination.

        Args:
            level: Filter by log level (optional).
            source: Filter by logger name (optional).
            correlation_id: Filter by correlation ID (optional).
            date_from: ISO 8601 lower bound for timestamp (inclusive, optional).
            date_to: ISO 8601 upper bound for timestamp (inclusive, optional).
            node_id: Filter by cluster node ID (optional).
            limit: Maximum number of records to return (default 100).
            offset: Number of records to skip for pagination (default 0).

        Returns:
            Tuple of (list_of_log_dicts, total_count) where total_count reflects
            the full match count before pagination is applied.
        """
        where_clause, params = self._build_query_conditions(
            level, source, correlation_id, date_from, date_to, node_id
        )

        with self._pool.connection() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM logs {where_clause}",
                params,
            ).fetchone()
            total_count: int = int(count_row[0]) if count_row else 0

            rows = conn.execute(
                f"""
                SELECT id, timestamp, level, source, message, correlation_id,
                       user_id, request_path, extra_data, node_id, created_at
                FROM logs {where_clause}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            ).fetchall()

        return [self._row_to_log_dict(row) for row in rows], total_count

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        """Delete log records older than days_to_keep days.

        Args:
            days_to_keep: Records with timestamp older than this many days are deleted.

        Returns:
            Number of rows deleted.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat()

        with self._pool.connection() as conn:
            result = conn.execute(
                "DELETE FROM logs WHERE timestamp < %s",
                (cutoff,),
            )
            deleted = int(result.rowcount) if result.rowcount else 0
            conn.commit()

        if deleted:
            logger.debug("LogsPostgresBackend: cleaned up %d old log records", deleted)
        return deleted

    def close(self) -> None:
        """No-op: pool lifecycle is managed externally."""
