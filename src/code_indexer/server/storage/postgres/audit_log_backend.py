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
from typing import Any, List, Optional, Sequence, Tuple

from code_indexer.server.services.audit_events import (
    AUDIT_ROW_COLUMNS,
    AuditEvent,
    build_legacy_event,
    event_row_values,
)
from code_indexer.server.services.audit_log_query import (
    POSTGRES_DIALECT,
    AuditFilters,
    build_aggregate_sql,
    build_count_sql,
    build_page_sql,
    build_terminal_rows_sql,
)

from code_indexer.server.services.siem_delivery.capture import (
    SiemDestinations,
    prepare_captures,
    record_transaction_failure,
    write_captures,
)
from code_indexer.server.services.siem_delivery.db import POSTGRES

from .pg_utils import sanitize_row


logger = logging.getLogger(__name__)

# PR-related action_type values (mirrors AuditLogService constants)
_PR_ACTION_TYPES = (
    "pr_creation_success",
    "pr_creation_failure",
    "pr_creation_disabled",
)

# Cleanup action_type value
_CLEANUP_ACTION_TYPE = "git_cleanup"

# Columns selected in every read query: the original seven plus the
# attribution columns (same names as the SQLite store).
_SELECT_COLS = (
    "id, timestamp, admin_id, action_type, target_type, target_id, details, "
    "outcome, source, ip_address, correlation_id, node_id, auth_method, "
    "actor_is_system, event_uuid"
)

_INSERT_EVENT_SQL = (
    f"INSERT INTO audit_logs ({', '.join(AUDIT_ROW_COLUMNS)}) "
    f"VALUES ({', '.join('%s' for _ in AUDIT_ROW_COLUMNS)})"
)


def _insert_event_rows(conn: Any, events: Sequence[AuditEvent]) -> None:
    """The ONLY statement the capture path uses to insert audit rows.

    Runs inside the caller's transaction.  A later capture step (for
    example a delivery-queue insert) can join the same transaction next to
    this call.
    """
    with conn.cursor() as cur:
        cur.executemany(
            _INSERT_EVENT_SQL, [event_row_values(event) for event in events]
        )


def _utc_row(row: dict) -> dict:
    """Row with every TIMESTAMPTZ value as an ISO-8601 UTC string.

    The session time zone must not leak into what readers see (or into a
    keyset cursor): the same instant always renders the same way.
    """
    return {
        key: (
            value.astimezone(timezone.utc).isoformat()
            if isinstance(value, datetime)
            else value
        )
        for key, value in row.items()
    }


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

    def insert_events(
        self,
        events: Sequence[AuditEvent],
        *,
        siem_destinations: Optional[SiemDestinations] = None,
    ) -> None:
        """Insert *events* in ONE transaction (not one commit per row).

        SIEM capture joins it: projection runs before the transaction, and
        each queue row is a savepointed (nested transaction), fail-open
        INSERT after the audit rows.  Raises on failure; the whole batch is
        rolled back.
        """
        if not events:
            return
        prepared = prepare_captures(events, siem_destinations)
        try:
            with self._conn() as conn:
                with conn.transaction():
                    _insert_event_rows(conn, events)
                    write_captures(conn, prepared, POSTGRES)
        except Exception as exc:
            record_transaction_failure(prepared, exc)
            raise

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
        self.insert_events(
            [
                build_legacy_event(
                    actor=admin_id,
                    action_type=action_type,
                    target_type=target_type,
                    target_id=target_id,
                    details_json=details,
                )
            ]
        )

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
        self.insert_events(
            [
                build_legacy_event(
                    actor=admin_id,
                    action_type=action_type,
                    target_type=target_type,
                    target_id=target_id,
                    details_json=details,
                    occurred_at=timestamp,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Shared read path (services/audit_log_query.py renders the SQL); the
    # ONE way rows are read for the Web page, MCP and REST.
    # ------------------------------------------------------------------

    def _fetch_dicts(self, sql: str, params: Sequence[Any]) -> List[dict]:
        with self._conn() as conn:
            with conn.cursor(row_factory=_dict_row_factory()) as cur:
                cur.execute(sql, list(params))
                return [_utc_row(row) for row in cur.fetchall()]

    def query_page(
        self,
        filters: AuditFilters,
        tier: str,
        *,
        seek: Optional[Tuple[str, int]],
        direction: str,
        limit: int,
        offset: int = 0,
    ) -> List[dict]:
        """One keyset page of rows (see ``audit_log_query.build_page_sql``)."""
        sql, params = build_page_sql(
            filters,
            tier,
            POSTGRES_DIALECT,
            seek=seek,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return self._fetch_dicts(sql, params)

    def count_capped(self, filters: AuditFilters, tier: str, *, cap: int) -> int:
        """Matching row count, reading at most ``cap + 1`` rows."""
        sql, params = build_count_sql(filters, tier, POSTGRES_DIALECT, cap=cap)
        return int(self._fetch_dicts(sql, params)[0]["cnt"])

    def aggregate(
        self, filters: AuditFilters, tier: str, *, max_groups: int
    ) -> List[dict]:
        """``GROUP BY (action_type, outcome)`` of the matching rows, in SQL."""
        sql, params = build_aggregate_sql(
            filters, tier, POSTGRES_DIALECT, max_groups=max_groups
        )
        return self._fetch_dicts(sql, params)

    def find_terminal_rows(self, correlation_ids: Sequence[str]) -> List[dict]:
        """Terminal rows sharing one of *correlation_ids* (pairing lookup)."""
        if not correlation_ids:
            return []
        sql, params = build_terminal_rows_sql(correlation_ids, POSTGRES_DIALECT)
        return self._fetch_dicts(sql, params)

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
                return [sanitize_row(row) for row in cur.fetchall()]

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
                return [sanitize_row(row) for row in cur.fetchall()]

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        """Delete audit log records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records before this are deleted.

        Returns:
            Number of rows deleted.
        """
        with self._conn() as conn:
            result = conn.execute(
                "DELETE FROM audit_logs WHERE timestamp < %s",
                (cutoff_iso,),
            )
            deleted = result.rowcount if result.rowcount else 0
            conn.commit()
        return deleted
