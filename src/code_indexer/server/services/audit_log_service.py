"""
AuditLogService - Dedicated audit log service for CIDX server.

Story #399: Audit Log Consolidation & AuditLogService Extraction

Owns the audit_logs SQLite table (extracted from GroupAccessManager).
Receives events from:
- GroupAccessManager call sites (group/user/repo admin actions)
- PasswordChangeAuditLogger (auth events)

Provides:
- log()            : Insert an audit event
- query()          : Filter/paginate audit events (replaces get_audit_logs)
- get_pr_logs()    : Query PR creation events (replaces flat-file parse)
- get_cleanup_logs(): Query git cleanup events (replaces flat-file parse)

Also exports:
- migrate_flat_file_to_sqlite(): One-shot startup migration from password_audit.log
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

# PR-related action_type values
_PR_ACTION_TYPES = (
    "pr_creation_success",
    "pr_creation_failure",
    "pr_creation_disabled",
)

# Cleanup action_type value
_CLEANUP_ACTION_TYPE = "git_cleanup"


class AuditLogService:
    """
    Service owning the audit_logs SQLite table.

    Extracted from GroupAccessManager (Story #399 AC1).
    Shares the same groups.db file — uses CREATE TABLE IF NOT EXISTS so it is
    safe to initialise alongside GroupAccessManager.
    """

    def __init__(self, db_path: Path, storage_backend: Any = None) -> None:
        self._backend = storage_backend
        if self._backend is not None:
            # PG mode: backend owns its own schema; skip SQLite init
            return
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(str(db_path))
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        return self._conn_manager.get_connection()  # type: ignore[no-any-return]

    def _ensure_schema(self) -> None:
        """Create audit_logs table and indexes if they don't exist."""

        def _do_schema(conn: sqlite3.Connection) -> None:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    admin_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_logs(timestamp DESC)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_action_type
                ON audit_logs(action_type)
                """
            )

        self._conn_manager.execute_atomic(_do_schema)

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
        Insert one audit log entry.

        Args:
            admin_id:    Actor performing the action (username or 'system').
            action_type: Verb describing what happened.
            target_type: Category of the target ('user', 'group', 'repo', 'auth').
            target_id:   Identifier of the specific target.
            details:     Optional JSON string with extra event data.
        """
        if self._backend is not None:
            self._backend.log(
                admin_id=admin_id,
                action_type=action_type,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
            return
        now = datetime.now(timezone.utc).isoformat()

        def _do_log(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO audit_logs
                    (timestamp, admin_id, action_type, target_type, target_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now, admin_id, action_type, target_type, target_id, details),
            )

        self._conn_manager.execute_atomic(_do_log)

    def log_raw(
        self,
        timestamp: str,
        admin_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        details: Optional[str] = None,
    ) -> None:
        """Insert an audit entry with an explicit timestamp (for migration use)."""
        if self._backend is not None:
            self._backend.log_raw(
                timestamp=timestamp,
                admin_id=admin_id,
                action_type=action_type,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
            return

        def _do_log_raw(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO audit_logs (timestamp, admin_id, action_type, target_type, target_id, details) VALUES (?, ?, ?, ?, ?, ?)",
                (timestamp, admin_id, action_type, target_type, target_id, details),
            )

        self._conn_manager.execute_atomic(_do_log_raw)

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
                                  Used by Groups UI to hide auth events (AC5).
            limit:                Max rows returned (None = unlimited).
            offset:               Rows to skip (for pagination).

        Returns:
            (list_of_dicts, total_matching_count)
        """
        if self._backend is not None:
            return self._backend.query(  # type: ignore[no-any-return]
                action_type=action_type,
                target_type=target_type,
                admin_id=admin_id,
                date_from=date_from,
                date_to=date_to,
                exclude_target_type=exclude_target_type,
                limit=limit,
                offset=offset,
            )

        conn = self._get_connection()
        conditions: List[str] = []
        params: List[Any] = []

        if action_type:
            conditions.append("action_type = ?")
            params.append(action_type)
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if admin_id:
            conditions.append("admin_id = ?")
            params.append(admin_id)
        if date_from:
            conditions.append("timestamp >= ?")
            params.append(f"{date_from}T00:00:00")
        if date_to:
            conditions.append("timestamp <= ?")
            params.append(f"{date_to}T23:59:59")
        if exclude_target_type:
            conditions.append("target_type != ?")
            params.append(exclude_target_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM audit_logs {where}", params)
        total = cursor.fetchone()["cnt"]

        query_sql = f"""
            SELECT id, timestamp, admin_id, action_type, target_type,
                   target_id, details
            FROM audit_logs
            {where}
            ORDER BY timestamp DESC
        """
        if limit is not None:
            query_sql += " LIMIT ? OFFSET ?"
            params = list(params) + [limit, offset]
        elif offset > 0:
            query_sql += " LIMIT -1 OFFSET ?"
            params = list(params) + [offset]

        cursor.execute(query_sql, params)
        rows = cursor.fetchall()

        logs = [dict(row) for row in rows]
        return logs, total

    def get_pr_logs(
        self,
        repo_alias: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query PR creation audit logs.

        Replaces PasswordChangeAuditLogger._parse_logs_by_prefix("PR_CREATION").

        Args:
            repo_alias: Filter by repository alias stored in target_id.
            limit:      Maximum records to return.
            offset:     Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        if self._backend is not None:
            return self._backend.get_pr_logs(  # type: ignore[no-any-return]
                repo_alias=repo_alias,
                limit=limit,
                offset=offset,
            )
        conn = self._get_connection()
        placeholders = ",".join("?" * len(_PR_ACTION_TYPES))
        conditions = [f"action_type IN ({placeholders})"]
        params: List[Any] = list(_PR_ACTION_TYPES)

        if repo_alias:
            conditions.append("target_id = ?")
            params.append(repo_alias)

        where = "WHERE " + " AND ".join(conditions)
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            f"""
            SELECT id, timestamp, admin_id, action_type, target_type,
                   target_id, details
            FROM audit_logs
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_cleanup_logs(
        self,
        repo_path: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        Query git cleanup audit logs.

        Replaces PasswordChangeAuditLogger._parse_logs_by_prefix("GIT_CLEANUP").

        Args:
            repo_path: Filter by repository path stored in target_id.
            limit:     Maximum records to return.
            offset:    Records to skip.

        Returns:
            List of audit log dicts (newest first).
        """
        if self._backend is not None:
            return self._backend.get_cleanup_logs(  # type: ignore[no-any-return]
                repo_path=repo_path,
                limit=limit,
                offset=offset,
            )
        conn = self._get_connection()
        conditions = ["action_type = ?"]
        params: List[Any] = [_CLEANUP_ACTION_TYPE]

        if repo_path:
            conditions.append("target_id = ?")
            params.append(repo_path)

        where = "WHERE " + " AND ".join(conditions)
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row  # type: ignore[assignment]
        cursor.execute(
            f"""
            SELECT id, timestamp, admin_id, action_type, target_type,
                   target_id, details
            FROM audit_logs
            {where}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        return [dict(row) for row in cursor.fetchall()]

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        """Delete audit log records older than cutoff_iso.

        Args:
            cutoff_iso: ISO 8601 timestamp; records with timestamp before
                        this value are deleted.

        Returns:
            Number of rows deleted.
        """
        if self._backend is not None:
            return self._backend.cleanup_old_logs(cutoff_iso)  # type: ignore[no-any-return]
        total_deleted = 0
        while True:
            batch: List[int] = [0]

            def _do_batch(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "DELETE FROM audit_logs WHERE rowid IN "
                    "(SELECT rowid FROM audit_logs WHERE timestamp < ? LIMIT 1000)",
                    (cutoff_iso,),
                )
                batch[0] = conn.execute("SELECT changes()").fetchone()[0]

            self._conn_manager.execute_atomic(_do_batch)
            if batch[0] == 0:
                break
            total_deleted += batch[0]
        return total_deleted


# ---------------------------------------------------------------------------
# Flat-file migration (AC4)
# ---------------------------------------------------------------------------


def _extract_actor(entry: dict) -> str:
    """Derive admin_id from a migrated flat-file entry."""
    # PR / cleanup events are system-originated
    event_type = entry.get("event_type", "")
    if event_type in _PR_ACTION_TYPES or event_type == _CLEANUP_ACTION_TYPE:
        return "system"
    # Auth events: use username, actor_username, or email as actor
    for key in ("username", "actor_username", "email", "client_id"):
        if entry.get(key):
            return str(entry[key])
    return "system"


def _extract_target_id(entry: dict) -> str:
    """Derive target_id from a migrated flat-file entry."""
    event_type = entry.get("event_type", "")
    if event_type in _PR_ACTION_TYPES:
        return str(entry.get("repo_alias") or entry.get("job_id") or "unknown")
    if event_type == _CLEANUP_ACTION_TYPE:
        return str(entry.get("repo_path") or "unknown")
    # Impersonation: target is the impersonated user
    if event_type in ("impersonation_set", "impersonation_cleared"):
        return str(
            entry.get("target_username") or entry.get("previous_target") or "unknown"
        )
    # Auth events: use username or email
    for key in ("username", "actor_username", "email"):
        if entry.get(key):
            return str(entry[key])
    return "unknown"


def migrate_flat_file_to_sqlite(
    log_file: Path,
    audit_service: AuditLogService,
) -> Tuple[int, int]:
    """
    One-shot migration: parse password_audit.log and insert into audit_logs.

    Idempotent: if the file doesn't exist, returns (0, 0) silently.
    Deletes the file after migration (even if all lines were malformed).

    Log line format: "YYYY-MM-DD HH:MM:SS UTC - LEVEL - PREFIX: {json}"

    Args:
        log_file:      Path to the flat log file.
        audit_service: Destination AuditLogService instance.

    Returns:
        (migrated_count, skipped_count)
    """
    if not log_file.exists():
        return 0, 0

    migrated = 0
    skipped = 0

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Find the first '{' — that's where JSON starts
                brace_pos = line.find("{")
                if brace_pos == -1:
                    skipped += 1
                    continue
                try:
                    entry = json.loads(line[brace_pos:])
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                event_type = entry.get("event_type")
                if not event_type:
                    skipped += 1
                    continue

                actor = _extract_actor(entry)
                target_id = _extract_target_id(entry)
                timestamp = (
                    entry.get("timestamp") or datetime.now(timezone.utc).isoformat()
                )
                details = json.dumps(entry)

                # All PasswordChangeAuditLogger events use target_type="auth" by design.
                # Events are distinguished by action_type, not target_type.
                audit_service.log_raw(
                    timestamp=timestamp,
                    admin_id=actor,
                    action_type=event_type,
                    target_type="auth",
                    target_id=target_id,
                    details=details,
                )
                migrated += 1

    except Exception as e:
        logger.warning(f"migrate_flat_file_to_sqlite: error reading {log_file}: {e}")

    # Always delete the file regardless of parse success
    try:
        log_file.unlink()
    except Exception as e:
        logger.warning(f"migrate_flat_file_to_sqlite: could not delete {log_file}: {e}")

    logger.info(
        f"Migrated {migrated} entries from {log_file.name}, "
        f"skipped {skipped} unparseable lines"
    )
    return migrated, skipped
