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
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

# Issue #1241 P1.3: async-batched audit writer constants.
# Audit durability matters: generous queue cap so saturation is rare.
_AUDIT_QUEUE_MAXSIZE = 50_000
# Max records to coalesce per drain cycle.
_AUDIT_MAX_DRAIN_BATCH = 512
# Poll timeout for the writer loop (seconds).
_AUDIT_POLL_TIMEOUT_S = 0.5
# How long stop() waits for the writer to drain (seconds).
_AUDIT_STOP_TIMEOUT_S = 10.0

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

        # Issue #1241 P1.3: async writer state (shared across both modes).
        # _writer_thread is None until start() is called; log() is synchronous
        # when the thread is not running (preserves backward-compat for tests
        # and callers that don't call start()).
        self._queue: queue.Queue = queue.Queue(maxsize=_AUDIT_QUEUE_MAXSIZE)
        self._stop_event: threading.Event = threading.Event()
        self._writer_thread: Optional[threading.Thread] = None

        if self._backend is not None:
            # PG mode: backend owns its own schema; skip SQLite init
            return
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(str(db_path))
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Lifecycle: start / flush / stop  (Issue #1241 P1.3)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background writer thread (async mode).

        After start() is called, log() and log_raw() enqueue items rather
        than writing synchronously.  Call stop() at shutdown to drain.
        """
        if self._writer_thread is not None and self._writer_thread.is_alive():
            return  # idempotent
        self._stop_event.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name="audit-log-writer",
        )
        self._writer_thread.start()

    def flush(self) -> None:
        """Synchronously drain the writer queue without stopping.

        Blocks until every record enqueued before this call has been
        committed by the writer thread.  No-op when the writer is not
        running (synchronous mode).
        """
        thread = self._writer_thread
        if thread is None or not thread.is_alive():
            return
        self._queue.join()

    def stop(self, timeout: float = _AUDIT_STOP_TIMEOUT_S) -> None:
        """Signal the writer to stop and wait for it to drain.

        Guarantees no audit rows are lost on graceful shutdown: the writer
        drains its queue before the thread exits.
        """
        self._stop_event.set()
        thread = self._writer_thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._writer_thread = None

    # ------------------------------------------------------------------
    # Internal: writer loop and batch write
    # ------------------------------------------------------------------

    def _write_batch(self, batch: List[Tuple]) -> None:
        """Write a batch of audit items in ONE transaction (executemany).

        Each item is a 6-tuple: (timestamp, admin_id, action_type,
        target_type, target_id, details).

        M3: all failures are logged at WARNING — never swallowed silently.
        M4: on executemany failure the SQLite path retries row-by-row so one
            poison row cannot drop up to 511 valid audit records.
        """
        if not batch:
            return
        if self._backend is not None:
            # PG/delegated path: call log_raw per item (backend handles
            # its own connection pooling and commit semantics).
            for ts, aid, at, tt, tid, det in batch:
                try:
                    self._backend.log_raw(
                        timestamp=ts,
                        admin_id=aid,
                        action_type=at,
                        target_type=tt,
                        target_id=tid,
                        details=det,
                    )
                except Exception as exc:
                    logger.warning(
                        "AuditLogService: PG log_raw failed (1 audit record dropped): %s",
                        exc,
                    )
            return
        # Direct-SQLite path: executemany in ONE transaction.
        rows = list(batch)

        def _do_batch(conn: sqlite3.Connection) -> None:
            conn.executemany(
                """
                INSERT INTO audit_logs
                    (timestamp, admin_id, action_type, target_type, target_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        try:
            self._conn_manager.execute_atomic(_do_batch)
        except Exception as exc:
            # M3: log so the failure is observable (audit subsystem must never
            #     swallow its own write errors silently).
            # M4: fall back to per-row inserts so one bad row cannot silently
            #     drop an entire batch of up to 512 valid audit records.
            logger.warning(
                "AuditLogService: batch insert failed (%d rows); "
                "retrying row-by-row: %s",
                len(rows),
                exc,
            )
            for row in rows:
                _row = row  # capture for closure

                def _do_single(conn: sqlite3.Connection, r: Tuple = _row) -> None:
                    conn.execute(
                        """
                        INSERT INTO audit_logs
                            (timestamp, admin_id, action_type, target_type,
                             target_id, details)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        r,
                    )

                try:
                    self._conn_manager.execute_atomic(_do_single)
                except Exception as row_exc:
                    logger.warning(
                        "AuditLogService: single-row fallback failed "
                        "(1 audit record dropped): %s | ts=%s action=%s",
                        row_exc,
                        row[0] if row else "?",
                        row[2] if len(row) > 2 else "?",
                    )

    def _writer_loop(self) -> None:
        """Background daemon: drain queue in batches and commit to DB.

        Runs until stop_event is set AND the queue is empty.
        """
        while True:
            try:
                first_item = self._queue.get(timeout=_AUDIT_POLL_TIMEOUT_S)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue

            batch = [first_item]
            additional = min(self._queue.qsize(), _AUDIT_MAX_DRAIN_BATCH - 1)
            for _ in range(additional):
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            self._write_batch(batch)

            for _ in batch:
                self._queue.task_done()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        return self._conn_manager.get_connection()  # type: ignore[no-any-return]

    def _ensure_schema(self) -> None:
        """Create audit_logs table and indexes if they don't exist."""
        # Issue #1241 P1.2: WAL must be set OUTSIDE any transaction.
        # SQLite silently ignores PRAGMA journal_mode = WAL if issued inside
        # BEGIN ... COMMIT (execute_atomic does BEGIN EXCLUSIVE).
        # Use a short-lived raw connection for this once-per-file pragma.
        # Note: busy_timeout is PER-CONNECTION and is set to 30000 ms by
        # DatabaseConnectionManager.get_connection() on every connection it
        # opens, so we do NOT set it here on this throwaway bootstrap connection.
        _bootstrap_conn = sqlite3.connect(str(self._db_path))
        try:
            _bootstrap_conn.execute("PRAGMA journal_mode = WAL")
            _bootstrap_conn.commit()
        finally:
            _bootstrap_conn.close()

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

    def _enqueue_or_write_sync(self, item: Tuple) -> None:
        """Enqueue if the writer thread is running, else write synchronously.

        This ensures backward compatibility: callers that never call start()
        get the original synchronous write behavior (existing tests pass
        unchanged).  Callers that call start() get non-blocking async writes.
        """
        thread = self._writer_thread
        if thread is not None and thread.is_alive():
            # Async mode: enqueue for background drain.
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                # Queue saturated (50k deep): write synchronously to preserve
                # audit durability — this record is too important to discard.
                self._write_batch([item])
        else:
            # Synchronous mode (not started): direct write — old behavior.
            self._write_batch([item])

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

        Issue #1241 P1.3: when start() has been called, this enqueues the
        record for async batched write (non-blocking).  Without start(), it
        writes synchronously (backward-compatible for tests and simple callers).

        Args:
            admin_id:    Actor performing the action (username or 'system').
            action_type: Verb describing what happened.
            target_type: Category of the target ('user', 'group', 'repo', 'auth').
            target_id:   Identifier of the specific target.
            details:     Optional JSON string with extra event data.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._enqueue_or_write_sync(
            (now, admin_id, action_type, target_type, target_id, details)
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
        """Insert an audit entry with an explicit timestamp (for migration use).

        Issue #1241 P1.3: async when writer thread is running, synchronous otherwise.
        """
        self._enqueue_or_write_sync(
            (timestamp, admin_id, action_type, target_type, target_id, details)
        )

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
