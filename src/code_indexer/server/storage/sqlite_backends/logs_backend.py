"""
SQLite backend for operational log storage (Story #500).

Split out of the monolithic sqlite_backends.py module (issue #1935 Part 1).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


class LogsSqliteBackend:
    """
    SQLite backend for operational log storage (Story #500).

    Stores log records written by SQLiteLogHandler (and cluster nodes) so
    the admin UI and REST API can query them with filtering and pagination.

    Uses a dedicated logs.db file (separate from the main cidx_server.db)
    to isolate high-volume log writes from other server state.
    """

    # Bug #1553: explicit capability flag -- this backend is node-local
    # (same file the writer and every same-process reader see), so read
    # dispatch must NOT route through it as a distinct cross-node store.
    is_cross_node_backend: bool = False

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend and create the logs table if it does not exist.

        Args:
            db_path: Path to SQLite database file (e.g. ~/.cidx-server/logs.db).
        """
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the logs table and indexes if they do not already exist."""

        def operation(conn: Any) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT,
                    message TEXT,
                    correlation_id TEXT,
                    user_id TEXT,
                    request_path TEXT,
                    extra_data TEXT,
                    node_id TEXT,
                    alias TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_correlation_id ON logs(correlation_id)"
            )
            # Migrate existing databases: add node_id / alias / trace_id /
            # span_id columns if missing (must run BEFORE creating the
            # indexes that reference them). Backward-compatible additive
            # change per the project's "Database Migrations Must Be
            # Backward Compatible" rule.
            cursor = conn.execute("PRAGMA table_info(logs)")
            columns = {row[1] for row in cursor.fetchall()}
            if "node_id" not in columns:
                conn.execute("ALTER TABLE logs ADD COLUMN node_id TEXT")
            # Story #876 Phase C: tag error rows with the repo alias so the
            # admin UI can filter lifecycle-runner failures by repo.
            if "alias" not in columns:
                conn.execute("ALTER TABLE logs ADD COLUMN alias TEXT")
            # Story #1676 AC2: OTEL trace/span correlation columns.
            if "trace_id" not in columns:
                conn.execute("ALTER TABLE logs ADD COLUMN trace_id TEXT")
            if "span_id" not in columns:
                conn.execute("ALTER TABLE logs ADD COLUMN span_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_node_id ON logs(node_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_alias ON logs(alias)")

        self._conn_manager.execute_atomic(operation)

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
        """Insert a single log record.

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
            alias: Optional repo alias (Story #876 Phase C). Tags rows written
                by the lifecycle-runner so operators can filter logs by repo.
            trace_id: Optional OTEL trace ID (Story #1676 AC2). 32-char hex,
                or the documented zero-value when no span was active.
            span_id: Optional OTEL span ID (Story #1676 AC2). 16-char hex,
                or the documented zero-value when no span was active.
        """

        def operation(conn: Any) -> None:
            conn.execute(
                """
                INSERT INTO logs
                    (timestamp, level, source, message, correlation_id,
                     user_id, request_path, extra_data, node_id, alias,
                     trace_id, span_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    alias,
                    trace_id,
                    span_id,
                ),
            )

        self._conn_manager.execute_atomic(operation)

    def insert_log_batch(self, items: List[Any]) -> bool:
        """Insert a batch of log records in ONE transaction via executemany.

        Issue #1241 P1.1: batched writer to eliminate per-record commit churn.

        Bug #1553: returns a real bool success signal (rather than implicit
        None) so SQLiteLogHandler's writer loop can detect failure without
        relying on an exception alone -- matching the sibling
        LogsPostgresBackend, which swallows its own failures internally and
        must report them the same way.

        Args:
            items: List of 12-tuples in column order (Story #1676 AC2 added
                trace_id/span_id):
                (timestamp, level, source, message, correlation_id,
                 user_id, request_path, extra_data, node_id, alias,
                 trace_id, span_id)

        Returns:
            True on success (including the empty-input no-op case).
        """
        if not items:
            return True

        def operation(conn: Any) -> None:
            conn.executemany(
                """
                INSERT INTO logs
                    (timestamp, level, source, message, correlation_id,
                     user_id, request_path, extra_data, node_id, alias,
                     trace_id, span_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                items,
            )

        self._conn_manager.execute_atomic(operation)
        return True

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
        """Build WHERE clause and params list for log queries.

        Bug #1553: levels/search are additive params mirroring
        LogAggregatorService._build_where_clause's semantics exactly
        (levels takes precedence over level; search is a case-insensitive
        substring match across message and correlation_id) so cluster reads
        routed through this backend keep the same filtering capability the
        standalone aggregator already offers.
        """
        conditions: List[str] = []
        params: List[Any] = []
        if levels:
            placeholders = ",".join(["?"] * len(levels))
            conditions.append(f"level IN ({placeholders})")
            params.extend(levels)
        elif level is not None:
            conditions.append("level = ?")
            params.append(level)
        if source is not None:
            conditions.append("source = ?")
            params.append(source)
        if correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)
        if date_from is not None:
            conditions.append("timestamp >= ?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("timestamp <= ?")
            params.append(date_to)
        if node_id is not None:
            conditions.append("node_id = ?")
            params.append(node_id)
        if search:
            conditions.append("(message LIKE ? OR correlation_id LIKE ?)")
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
            "created_at": row[13],
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

        Bug #1553: levels/search/sort_order are additive params -- their
        defaults preserve the exact pre-existing behaviour for every caller
        that predates them (single-level equality, no text search, DESC).

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
        conn = self._conn_manager.get_connection()
        total_count: int = conn.execute(
            f"SELECT COUNT(*) FROM logs {where_clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, timestamp, level, source, message, correlation_id,
                   user_id, request_path, extra_data, node_id, alias,
                   trace_id, span_id, created_at
            FROM logs {where_clause}
            ORDER BY timestamp {order_direction}
            LIMIT ? OFFSET ?
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

        def operation(conn: Any) -> int:
            cursor = conn.execute(
                "DELETE FROM logs WHERE timestamp < ?",
                (cutoff,),
            )
            return int(cursor.rowcount)

        deleted: int = self._conn_manager.execute_atomic(operation)
        if deleted:
            logger.debug("Cleaned up %d old log records", deleted)
        return deleted

    def close(self) -> None:
        """No-op: connections are managed by DatabaseConnectionManager."""
        pass
