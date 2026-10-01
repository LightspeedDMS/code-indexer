"""
Regression test for Bug #1532: close_all() racing an in-flight execute_atomic().

Symptom (from the issue): server-fast-automation.sh intermittently segfaults
(exit 139) with fatal-thread stacks showing one thread inside
DatabaseConnectionManager.close_all() (via a backend's close(), called from
migration_service.migrate_ssh_keys) while another thread is inside
SQLiteLogHandler._writer_loop performing a DB write via execute_atomic() on
the SAME DatabaseConnectionManager instance.

Root cause: close_all() closes every tracked sqlite3.Connection without any
synchronization against a concurrent execute_atomic() call using that same
connection on another thread. sqlite3 connections created with
check_same_thread=False permit cross-thread access at the Python level, but
closing a connection out from under an in-flight multi-statement operation on
another thread is unsafe at the C/SQLite layer -- in CI this manifested as an
intermittent SIGSEGV; in a tighter, deterministic repro it manifests as
sqlite3.ProgrammingError ("Cannot operate on a closed database") raised from
inside the operation after close_all() has already closed the connection out
from under it.

This test drives the exact code path (get_instance -> execute_atomic on a
background thread, close_all() on the main thread) with a deliberate sleep
inside the operation to widen the race window deterministically, and asserts
execute_atomic() never observes a closed connection.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import List

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

# Time the slow_operation sleeps inside its transaction, simulating the
# writer thread doing real work between BEGIN and COMMIT.  Must be long
# enough that the main thread's close_all() call below is guaranteed to
# overlap it every run (deterministic race window, not a flaky sleep race).
_OPERATION_HOLD_SECONDS = 0.3

# Delay after `started` fires before the main thread calls close_all(), to
# make sure the writer thread is genuinely inside its hold-sleep (and not
# merely about to enter it) before the race is triggered.
_PRE_CLOSE_SETTLE_SECONDS = 0.05

# Bounded waits for the two thread-coordination points below.
_STARTED_WAIT_TIMEOUT_SECONDS = 2.0
_WRITER_JOIN_TIMEOUT_SECONDS = 5.0

# Arbitrary row value written by the racing operation; its content is
# irrelevant, only that the write completes without error.
_TEST_ROW_ID = 1


def test_close_all_does_not_race_with_in_flight_execute_atomic(tmp_path: Path) -> None:
    """close_all() must not close a connection while another thread is
    mid-transaction inside execute_atomic() on that same connection."""
    db_path = tmp_path / "race_1532.db"
    mgr = DatabaseConnectionManager.get_instance(str(db_path))

    # Bootstrap schema on the main thread's own connection.
    mgr.execute_atomic(
        lambda conn: conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")
    )

    started = threading.Event()
    errors: List[Exception] = []

    def slow_operation(conn: sqlite3.Connection) -> None:
        started.set()
        # Widen the race window: close_all() (called from the main thread
        # right after `started` is set) must block until this operation
        # finishes, never close the connection while this sleep is running.
        time.sleep(_OPERATION_HOLD_SECONDS)
        conn.execute("INSERT INTO t (id) VALUES (?)", (_TEST_ROW_ID,))

    def writer() -> None:
        try:
            mgr.execute_atomic(slow_operation)
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        try:
            assert started.wait(timeout=_STARTED_WAIT_TIMEOUT_SECONDS), (
                "writer thread never entered execute_atomic"
            )
            # Give the writer thread a moment to genuinely be inside the
            # sleep, holding whatever synchronization the fix relies on,
            # before we race it.
            time.sleep(_PRE_CLOSE_SETTLE_SECONDS)
        finally:
            # This must block until slow_operation() completes rather than
            # closing the connection out from under it. Attempted even if
            # the wait above failed/timed out, so the writer thread is
            # never left holding a connection nobody will close.
            mgr.close_all()
    finally:
        writer_thread.join(timeout=_WRITER_JOIN_TIMEOUT_SECONDS)

    assert not writer_thread.is_alive(), "writer thread did not finish"

    assert not errors, (
        "execute_atomic() raised while racing close_all() -- connection was "
        f"closed out from under an in-flight operation: {errors}"
    )
