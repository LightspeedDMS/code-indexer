"""
PostgreSQL backend for operational log storage (Story #501).

Drop-in replacement for LogsSqliteBackend using psycopg v3 sync connections
via ConnectionPool.  Satisfies the LogsBackend Protocol (protocols.py).

Schema (`logs` table and its indexes) is owned entirely by the SQL
migrations (storage/postgres/migrations/sql/020_logs_alias_column.sql,
048_logs_trace_span_columns.sql) -- this backend does NOT create or alter
any table. `service_init.py` always runs `MigrationRunner` before
`StorageFactory.create_backends()` constructs this class, so schema is
guaranteed present by the time any instance exists (Issue #1697, mirroring
Bug #1655/#1662: a previous self-heal `CREATE TABLE IF NOT EXISTS` here was
dead code in every real deployment -- removed rather than kept as a second,
drift-prone copy of the schema).

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

    # Bug #1553: explicit capability flag -- this backend IS a distinct
    # cross-node store (PostgreSQL, shared across every cluster node), so
    # read dispatch must route through it rather than a node-local file.
    is_cross_node_backend: bool = True

    def __init__(self, pool: ConnectionPool) -> None:
        """
        Initialize with a shared connection pool.

        Schema is assumed to already exist (see module docstring) -- this
        constructor does not touch the database.

        Args:
            pool: ConnectionPool instance providing psycopg v3 connections.
        """
        self._pool = pool

    # Story #1676 AC2: shared column list for the logs table's INSERT
    # statements, so insert_log/insert_log_batch stay in sync with each
    # other and with the migration-owned schema (single source of truth).
    _INSERT_COLUMNS = (
        "timestamp, level, source, message, correlation_id, "
        "user_id, request_path, extra_data, node_id, alias, "
        "trace_id, span_id"
    )
    _INSERT_PLACEHOLDERS = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"

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
        alias: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
    ) -> None:
        """Insert a single log record (see class docstring for the
        node_id/alias/trace_id/span_id column semantics). Failures are
        caught and logged as warnings -- a failed log write must never crash
        the application."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    f"INSERT INTO logs ({self._INSERT_COLUMNS}) "
                    f"VALUES ({self._INSERT_PLACEHOLDERS})",
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
                        alias,
                        trace_id,
                        span_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("LogsPostgresBackend: insert_log failed: %s", exc)

    def insert_log_batch(self, items: List[Any]) -> bool:
        """Insert a batch of log records in ONE transaction via executemany.

        Issue #1241 P1.1: batched writer to eliminate per-record commit churn.
        Uses SET LOCAL synchronous_commit = off (safe: rows are immediately
        visible; only crash-flush durability is relaxed).

        Bug #1553: returns a real bool success signal so SQLiteLogHandler's
        writer loop can detect failure without relying on an exception alone.

        Args:
            items: List of 12-tuples matching _INSERT_COLUMNS' order (Story
                #1676 AC2 appended trace_id/span_id).

        Returns:
            True on success (including the empty-input no-op case), False
            if the insert failed (already logged as a warning internally).
        """
        if not items:
            return True
        try:
            with self._pool.connection() as conn:
                # psycopg v3: executemany lives on the CURSOR, NOT the connection
                # (mirrors PayloadCachePostgresBackend/QueryEmbeddingCachePostgresBackend).
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL synchronous_commit = off")
                    cur.executemany(
                        f"INSERT INTO logs ({self._INSERT_COLUMNS}) "
                        f"VALUES ({self._INSERT_PLACEHOLDERS})",
                        items,
                    )
                conn.commit()
            return True
        except Exception as exc:
            logger.warning("LogsPostgresBackend: insert_log_batch failed: %s", exc)
            return False

    def _build_query_conditions(
        self,
        level: Optional[str],
        source: Optional[str],
        correlation_id: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
        node_id: Optional[str],
        levels: Optional[List[str]] = None,
        search: Optional[str] = None,
    ) -> Tuple[str, List[Any]]:
        """Build WHERE clause and params list for log queries (parameterized).

        Bug #1553: levels/search are additive, mirroring
        LogAggregatorService._build_where_clause (levels takes precedence
        over level, guarded by `if levels:` so an empty list never emits
        invalid SQL like `level IN ()`; search is a case-insensitive
        substring match across message/correlation_id via ILIKE -- plain
        LIKE is case-SENSITIVE in PostgreSQL, unlike SQLite's default).
        """
        conditions: List[str] = []
        params: List[Any] = []
        if levels:
            placeholders = ",".join(["%s"] * len(levels))
            conditions.append(f"level IN ({placeholders})")
            params.extend(levels)
        elif level is not None:
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
        if search:
            conditions.append("(message ILIKE %s OR correlation_id ILIKE %s)")
            search_pattern = f"%{search}%"
            params.append(search_pattern)
            params.append(search_pattern)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where_clause, params

    def _row_to_log_dict(self, row: tuple) -> Dict[str, Any]:
        """Convert a database row tuple to a log record dict.

        Column order matches the SELECT list in query_logs(): id, timestamp,
        level, source, message, correlation_id, user_id, request_path,
        extra_data, node_id, alias, trace_id, span_id, created_at
        (Story #1676 AC2 appended trace_id/span_id).
        """
        # created_at may come back as a datetime from PostgreSQL
        created_at = row[13]
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
            "alias": row[10],
            "trace_id": row[11],
            "span_id": row[12],
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
        levels: Optional[List[str]] = None,
        search: Optional[str] = None,
        sort_order: str = "desc",
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Query log records with optional filtering and pagination.

        Bug #1553: levels/search/sort_order are additive -- their defaults
        preserve the exact pre-existing behaviour for every caller that
        predates them (single-level equality, no text search, DESC).

        Returns:
            Tuple of (list_of_log_dicts, total_count) where total_count reflects
            the full match count before pagination is applied.
        """
        where_clause, params = self._build_query_conditions(
            level,
            source,
            correlation_id,
            date_from,
            date_to,
            node_id,
            levels,
            search,
        )
        order_direction = "ASC" if sort_order == "asc" else "DESC"

        with self._pool.connection() as conn:
            count_row = conn.execute(
                f"SELECT COUNT(*) FROM logs {where_clause}",
                params,
            ).fetchone()
            total_count: int = int(count_row[0]) if count_row else 0

            rows = conn.execute(
                f"""
                SELECT id, timestamp, level, source, message, correlation_id,
                       user_id, request_path, extra_data, node_id, alias,
                       trace_id, span_id, created_at
                FROM logs {where_clause}
                ORDER BY timestamp {order_direction}
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
