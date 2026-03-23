"""
PostgreSQL backend for AuditLogService storage (AuditLogBackend Protocol).

Story #415: PostgreSQL Backend Migration

Implements the AuditLogBackend Protocol using psycopg v3 (sync mode).
The audit_logs table lives in the main PostgreSQL database.

Usage:
    from code_indexer.server.storage.postgres.audit_log_backend import AuditLogPostgresBackend

    backend = AuditLogPostgresBackend(pool)
    backend.log("admin", "user_created", "user", "alice")
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple


logger = logging.getLogger(__name__)

# PR-related action_type values (mirrors AuditLogService constants)
_PR_ACTION_TYPES = (
    "pr_creation_success",
    "pr_creation_failure",
    "pr_creation_disabled",
)

# Cleanup action_type value
_CLEANUP_ACTION_TYPE = "git_cleanup"

# Columns selected in every read query
_SELECT_COLS = "id, timestamp, admin_id, action_type, target_type, target_id, details"


def _dict_row_factory() -> Any:
    """Return psycopg v3 dict_row row factory, loaded lazily."""
    try:
        from psycopg.rows import dict_row

        return dict_row
    except ImportError as exc:
        raise ImportError(
            "psycopg (v3) is required for AuditLogPostgresBackend. "
            "Install with: pip install psycopg"
        ) from exc


class AuditLogPostgresBackend:
    """
    PostgreSQL implementation of the AuditLogBackend Protocol.

    Manages audit_logs in the main PostgreSQL database via a psycopg v3
    connection pool.  Drop-in replacement for the SQLite-backed AuditLogService
    when the server is configured to use PostgreSQL.
    """

    def __init__(self, pool: Any) -> None:
        """
        Args:
            pool: An open psycopg v3 ConnectionPool instance.
        """
        self._pool = pool

    def _conn(self) -> Any:
        """Borrow a connection from the pool (context manager)."""
        return self._pool.connection()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log(
        self,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        """
        Insert one audit log entry with the current UTC timestamp.

        Args:
            admin_id:    Actor performing the action (username or 'system').
            action_type: Verb describing what happened.
            target_type: Category of the target ('user', 'group', 'repo', 'auth').
            target_id:   Identifier of the specific target.
            details:     Optional JSON string with extra event data.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_logs "
                    "(timestamp, admin_id, action_type, target_type, target_id, details) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (now, admin_id, action_type, target_type, target_id, details),
                )
            conn.commit()

    def log_raw(
        self,
        timestamp: str,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        """
        Insert an audit entry with an explicit timestamp (for migration use).

        Args:
            timestamp:   ISO-format UTC timestamp string.
            admin_id:    Actor performing the action.
            action_type: Verb describing what happened.
            target_type: Category of the target.
            target_id:   Identifier of the specific target.
            details:     Optional JSON string with extra event data.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_logs "
                    "(timestamp, admin_id, action_type, target_type, target_id, details) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (timestamp, admin_id, action_type, target_type, target_id, details),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def query(
        self,
        action_type: Optional[str] = None,
        target_type: Optional[str] = None,
        admin_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        exclude_target_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Tuple[List[dict], int]:
        """
        Query audit log entries with optional filters.

        Args:
            action_type:          Filter by exact action_type.
            target_type:          Filter by exact target_type.
            admin_id:             Filter by exact admin_id.
            date_from:            ISO date string YYYY-MM-DD (inclusive lower bound).
            date_to:              ISO date string YYYY-MM-DD (inclusive upper bound).
            exclude_target_type:  Exclude rows where target_type equals this value.
            limit:                Max rows returned (None = unlimited).
            offset:               Rows to skip (for pagination).

        Returns:
            (list_of_dicts, total_matching_count)
        """
        conditions: List[str] = []
        params: List[Any] = []

        if action_type:
            conditions.append("action_type = %s")
            params.append(action_type)
        if target_type:
            conditions.append("target_type = %s")
            params.append(target_type)
        if admin_id:
            conditions.append("admin_id = %s")
            params.append(admin_id)
        if date_from:
            conditions.append("timestamp >= %s")
            params.append(f"{date_from}T00:00:00")
        if date_to:
            conditions.append("timestamp <= %s")
            params.append(f"{date_to}T23:59:59")
        if exclude_target_type:
            conditions.append("target_type != %s")
            params.append(exclude_target_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS cnt FROM audit_logs {where}",
                    params,
                )
                count_row = cur.fetchone()
                total: int = count_row["cnt"] if count_row else 0

                query_sql = (
                    f"SELECT {_SELECT_COLS} FROM audit_logs {where} "
                    "ORDER BY timestamp DESC"
                )
                count_params = list(params)
                if limit is not None:
                    query_sql += " LIMIT %s OFFSET %s"
                    count_params = count_params + [limit, offset]
                elif offset > 0:
                    query_sql += " OFFSET %s"
                    count_params = count_params + [offset]

                cur.execute(query_sql, count_params)
                rows = cur.fetchall()

        return list(rows), total

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query PR creation audit logs.

        Args:
            repo_alias: Filter by repository alias stored in target_id.
            limit:      Maximum records to return.
            offset:     Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        placeholders = ",".join(["%s"] * len(_PR_ACTION_TYPES))
        conditions = [f"action_type IN ({placeholders})"]
        params: List[Any] = list(_PR_ACTION_TYPES)

        if repo_alias:
            conditions.append("target_id = %s")
            params.append(repo_alias)

        where = "WHERE " + " AND ".join(conditions)

        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM audit_logs {where} "
                    "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                    params + [limit, offset],
                )
                return list(cur.fetchall())

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query git cleanup audit logs.

        Args:
            repo_path: Filter by repository path stored in target_id.
            limit:     Maximum records to return.
            offset:    Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        conditions = ["action_type = %s"]
        params: List[Any] = [_CLEANUP_ACTION_TYPE]

        if repo_path:
            conditions.append("target_id = %s")
            params.append(repo_path)

        where = "WHERE " + " AND ".join(conditions)

        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(
                    f"SELECT {_SELECT_COLS} FROM audit_logs {where} "
                    "ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                    params + [limit, offset],
                )
                return list(cur.fetchall())
