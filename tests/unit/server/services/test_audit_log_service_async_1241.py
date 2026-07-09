"""Tests for Issue #1241 P1.3: AuditLogService async-batched writer.

These tests assert that:
1. audit_log_service.log() / log_raw() do NOT perform DB I/O on the calling
   thread — the write is enqueued and committed by a background writer thread.
2. flush() (or stop()) drains the queue completely; no rows are lost.
3. The audit DB connection uses WAL journal_mode and busy_timeout > 0.
4. Graceful shutdown via stop() loses no audit records.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_audit_service(db_path: Path) -> Any:
    from code_indexer.server.services.audit_log_service import AuditLogService

    svc = AuditLogService(db_path)
    svc.start()
    return svc


def _count_rows(db_path: Path) -> int:
    """Read audit_logs row count directly via sqlite3 (bypasses any cache)."""
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test 1: log() does NOT block on DB I/O on the calling thread
# ---------------------------------------------------------------------------


def test_audit_log_does_not_block_on_calling_thread(tmp_path: Path) -> None:
    """AuditLogService.log() must enqueue the record and return immediately.

    A slow DB write must NOT cause log() to block: the DB write must happen
    on a background writer thread, not the caller.
    """
    from code_indexer.server.services.audit_log_service import AuditLogService

    db_path = tmp_path / "audit.db"
    svc = AuditLogService(db_path)
    svc.start()
    try:
        # Time a burst of log() calls — they should all return very quickly
        # (non-blocking enqueue) regardless of how fast the DB writes go.
        t_start = time.monotonic()
        for i in range(100):
            svc.log(
                admin_id="admin",
                action_type="test_action",
                target_type="user",
                target_id=f"user-{i}",
                details=f'{{"index": {i}}}',
            )
        elapsed_ms = (time.monotonic() - t_start) * 1000

        # 100 non-blocking enqueues must complete in well under 1 second
        assert elapsed_ms < 1000, (
            f"100 audit log() calls took {elapsed_ms:.1f} ms — "
            "they appear to be blocking on DB I/O (expected < 1000 ms for "
            "non-blocking enqueue)."
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Test 2: log() record appears after flush() — background writer commits it
# ---------------------------------------------------------------------------


def test_audit_log_row_appears_after_flush(tmp_path: Path) -> None:
    """After flush(), the enqueued audit record must be present in the DB."""
    db_path = tmp_path / "audit.db"
    svc = _build_audit_service(db_path)
    try:
        svc.log(
            admin_id="admin",
            action_type="create_user",
            target_type="user",
            target_id="alice",
            details='{"role": "viewer"}',
        )

        # WITHOUT flush, the row may not yet be in DB (it's queued)
        # WITH flush, it must be present
        svc.flush()

        count = _count_rows(db_path)
        assert count == 1, (
            f"Expected 1 audit row after flush(), got {count}. "
            "The background writer did not commit the row."
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Test 3: log_raw() also async — row present after flush
# ---------------------------------------------------------------------------


def test_audit_log_raw_row_appears_after_flush(tmp_path: Path) -> None:
    """log_raw() must also enqueue asynchronously; row present after flush."""
    db_path = tmp_path / "audit_raw.db"
    svc = _build_audit_service(db_path)
    try:
        svc.log_raw(
            timestamp="2024-01-01T12:00:00+00:00",
            admin_id="system",
            action_type="migration",
            target_type="auth",
            target_id="flat-file",
            details='{"migrated": 42}',
        )

        svc.flush()
        count = _count_rows(db_path)
        assert count == 1, f"Expected 1 row after log_raw() + flush(), got {count}."

        # Verify exact timestamp is preserved
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT timestamp, admin_id, action_type FROM audit_logs"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row[0] == "2024-01-01T12:00:00+00:00"
        assert row[1] == "system"
        assert row[2] == "migration"
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Test 4: stop() drains the queue — no rows lost on graceful shutdown
# ---------------------------------------------------------------------------


def test_audit_stop_loses_no_rows(tmp_path: Path) -> None:
    """stop() must drain the queue before joining the writer thread.

    All enqueued records must be in the DB after stop() returns.
    """
    db_path = tmp_path / "audit_stop.db"
    svc = _build_audit_service(db_path)

    n = 200
    for i in range(n):
        svc.log(
            admin_id="admin",
            action_type="bulk_op",
            target_type="repo",
            target_id=f"repo-{i}",
        )

    # stop() must drain everything before returning
    svc.stop()

    count = _count_rows(db_path)
    assert count == n, (
        f"Expected {n} rows after stop(), got {count}. "
        f"{n - count} records were lost during shutdown drain."
    )


# ---------------------------------------------------------------------------
# Test 5: WAL and busy_timeout on audit DB connection
# ---------------------------------------------------------------------------


def test_audit_db_wal_and_busy_timeout(tmp_path: Path) -> None:
    """After AuditLogService initialises:
    - journal_mode must be 'wal' (set once at bootstrap, persists DB-wide).
    - busy_timeout must be 30000 ms on every connection opened by
      DatabaseConnectionManager (set per-connection, NOT on the throwaway
      bootstrap connection — asserting it on a raw sqlite3.connect() is a
      tautology; we assert the manager's value here instead).
    """
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db_path = tmp_path / "audit_pragma.db"
    svc = _build_audit_service(db_path)
    try:
        # WAL is DB-level persistent: a fresh raw connection sees it.
        raw_conn = sqlite3.connect(str(db_path))
        try:
            journal_mode = raw_conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            raw_conn.close()

        assert journal_mode == "wal", (
            f"audit DB journal_mode = '{journal_mode}', expected 'wal'. "
            "WAL prevents the writer thread from blocking reader queries."
        )

        # busy_timeout is PER-CONNECTION; DatabaseConnectionManager sets 30000 ms.
        # Assert via the manager's own connection (not the throwaway bootstrap).
        mgr = DatabaseConnectionManager.get_instance(str(db_path))
        mgr_conn = mgr.get_connection()
        busy_timeout = mgr_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(busy_timeout) == 30000, (
            f"DatabaseConnectionManager connection busy_timeout = {busy_timeout}, "
            "expected 30000. The manager must set busy_timeout=30000 per connection."
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Test 6: calling thread performs NO DB I/O (thread ID assertion)
# ---------------------------------------------------------------------------


def test_audit_log_writer_thread_not_caller_thread(tmp_path: Path) -> None:
    """The audit write must happen on a dedicated writer thread, not caller.

    Method: intercept execute_atomic and record which thread called it.
    The audit log() call must NOT trigger execute_atomic on the calling thread.
    """
    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.storage.database_manager import DatabaseConnectionManager

    db_path = tmp_path / "audit_thread.db"
    svc = AuditLogService(db_path)
    svc.start()

    # Spy on execute_atomic to record calling thread names
    mgr = DatabaseConnectionManager.get_instance(str(db_path))
    original_execute_atomic = mgr.execute_atomic
    write_thread_names: List[str] = []

    def spy_execute_atomic(op: Any) -> Any:
        write_thread_names.append(threading.current_thread().name)
        return original_execute_atomic(op)

    # Patch AFTER start (schema init already done)
    mgr.execute_atomic = spy_execute_atomic  # type: ignore[method-assign]

    caller_thread = threading.current_thread().name
    try:
        svc.log(
            admin_id="spy-admin",
            action_type="spy_action",
            target_type="user",
            target_id="spy-user",
        )

        # The call to log() must return before execute_atomic fires
        # (it's enqueued, not synchronous)
        assert caller_thread not in write_thread_names, (
            f"execute_atomic was called on the CALLER thread '{caller_thread}' "
            "immediately after log() — the write is SYNCHRONOUS (not async). "
            "Expected the write to be enqueued and committed by the writer thread."
        )

        svc.flush()

        # After flush, the write must have happened on a DIFFERENT thread
        assert len(write_thread_names) > 0, (
            "execute_atomic was never called — the audit record was not written."
        )
        for thread_name in write_thread_names:
            assert thread_name != caller_thread, (
                f"execute_atomic ran on the caller thread '{caller_thread}' — "
                "audit log() is still synchronous."
            )
    finally:
        mgr.execute_atomic = original_execute_atomic  # type: ignore[method-assign]
        svc.stop()
