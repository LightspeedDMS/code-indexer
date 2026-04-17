"""
Unit tests for SQLiteLogHandler connection management.

Story #369: Fix SQLite Thread-Local Connection Leak
Bug #435: Remaining sqlite3.connect() calls migrated to DatabaseConnectionManager

The SQLiteLogHandler now delegates all connection management to
DatabaseConnectionManager. These tests verify:
- _get_connection() returns a real usable connection
- emit() writes correctly across multiple threads
- close() is safe and idempotent
- The handler delegates to DatabaseConnectionManager (not its own pool)

Uses real SQLite connections and real threads - zero mocking.
"""

import logging
import sqlite3
import threading
from typing import Callable, List, Tuple, Union
import pytest
from pathlib import Path


@pytest.mark.slow
class TestSQLiteLogHandlerCleanup:
    """Tests for connection management in SQLiteLogHandler."""

    def test_get_connection_returns_real_connection(self, tmp_path: Path) -> None:
        """
        _get_connection() delegates to DatabaseConnectionManager and returns
        a real sqlite3.Connection that can execute SQL.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        conn = handler._get_connection()
        assert conn is not None
        # Connection must be usable
        result = conn.execute("SELECT 1").fetchone()
        assert result == (1,)
        handler.close()

    def test_get_connection_same_connection_within_thread(self, tmp_path: Path) -> None:
        """
        _get_connection() returns the same connection object for repeated
        calls from the same thread (thread-local semantics via manager).
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        conn1 = handler._get_connection()
        conn2 = handler._get_connection()
        assert conn1 is conn2
        handler.close()

    def test_different_threads_get_independent_connections(
        self, tmp_path: Path
    ) -> None:
        """
        Threads get independent connections through DatabaseConnectionManager's
        thread-local storage.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        main_conn = handler._get_connection()
        thread_conn_holder: list = []

        def get_conn_in_thread() -> None:
            thread_conn_holder.append(handler._get_connection())

        t = threading.Thread(target=get_conn_in_thread)
        t.start()
        t.join()

        assert len(thread_conn_holder) == 1
        # Thread got a real connection
        assert thread_conn_holder[0] is not None
        # Thread's connection is independent from main thread's
        assert thread_conn_holder[0] is not main_conn
        handler.close()

    def test_concurrent_emit_from_multiple_threads(self, tmp_path: Path) -> None:
        """
        Multiple threads can emit log records concurrently without errors.
        All records must be written to the database.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        num_threads = 4
        records_per_thread = 5
        errors: list = []

        def emit_records(thread_idx: int) -> None:
            logger = logging.getLogger(f"thread_{thread_idx}")
            for i in range(records_per_thread):
                try:
                    record = logger.makeRecord(
                        name=f"thread_{thread_idx}",
                        level=logging.INFO,
                        fn="test_file.py",
                        lno=i,
                        msg=f"Thread {thread_idx} record {i}",
                        args=(),
                        exc_info=None,
                    )
                    handler.emit(record)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=emit_records, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        handler.close()

        assert errors == [], f"Errors during concurrent emit: {errors}"

        # Verify all records were written
        conn = sqlite3.connect(str(tmp_path / "test_logs.db"))
        count = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        conn.close()

        assert count == num_threads * records_per_thread

    def test_close_is_safe_and_idempotent(self, tmp_path: Path) -> None:
        """
        close() can be called multiple times without raising errors.
        Connection management is owned by DatabaseConnectionManager.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")
        handler._get_connection()

        # close() must not raise
        handler.close()
        handler.close()  # second call must also be safe

    def test_handler_delegates_to_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """
        SQLiteLogHandler uses DatabaseConnectionManager for connections,
        not its own internal pool. Verify by checking manager has a connection
        for this db after _get_connection() is called.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = tmp_path / "test_logs.db"
        handler = SQLiteLogHandler(db_path)
        conn_from_handler = handler._get_connection()

        # DatabaseConnectionManager should have the same connection for this thread
        conn_from_manager = DatabaseConnectionManager.get_instance(
            str(db_path)
        ).get_connection()
        assert conn_from_handler is conn_from_manager

        handler.close()

    def test_emit_still_works_after_adding_tracking(self, tmp_path: Path) -> None:
        """
        Verify emit() still works correctly after connection management
        was delegated to DatabaseConnectionManager.
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        handler = SQLiteLogHandler(tmp_path / "test_logs.db")

        # Create a log record and emit it
        logger = logging.getLogger("test_emit")
        record = logger.makeRecord(
            name="test_emit",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

        # Verify it was written to the DB
        conn = sqlite3.connect(str(tmp_path / "test_logs.db"))
        rows = conn.execute("SELECT message FROM logs").fetchall()
        conn.close()

        assert len(rows) == 1
        assert "Test message" in rows[0][0]
        handler.close()


class TestEmitUsesExecuteAtomic:
    """
    Bug #435 fix: emit() must delegate its INSERT to execute_atomic()
    on DatabaseConnectionManager, not call conn.commit() directly on the
    shared thread-local connection.

    Using raw conn.commit() on the shared connection can commit or roll back
    transactions belonging to other callers on the same thread.
    execute_atomic() provides proper transaction isolation for each INSERT.
    """

    def test_emit_calls_execute_atomic_not_raw_commit(self, tmp_path: Path) -> None:
        """
        emit() must call execute_atomic() on DatabaseConnectionManager,
        not raw conn.commit().
        """
        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = tmp_path / "test_emit_atomic.db"
        handler = SQLiteLogHandler(db_path)

        atomic_calls: list = []
        real_manager = DatabaseConnectionManager.get_instance(str(db_path))
        real_execute_atomic = real_manager.execute_atomic

        def tracking_execute_atomic(fn):
            atomic_calls.append(fn)
            return real_execute_atomic(fn)

        real_manager.execute_atomic = tracking_execute_atomic

        logger_inst = logging.getLogger("test_atomic")
        record = logger_inst.makeRecord(
            name="test_atomic",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="Atomic test message",
            args=(),
            exc_info=None,
        )

        try:
            handler.emit(record)
        finally:
            real_manager.execute_atomic = real_execute_atomic
            handler.close()

        assert len(atomic_calls) >= 1, (
            "emit() must call execute_atomic() for the INSERT; "
            "raw conn.commit() was used instead"
        )

    def test_emit_does_not_call_conn_commit_directly(self, tmp_path: Path) -> None:
        """
        emit() must not call conn.commit() directly on the shared connection.
        Uses MagicMock(wraps=real_conn) to track commit() calls while
        still executing real SQL (Python 3.9: commit is a read-only C descriptor).
        """
        from unittest.mock import MagicMock, patch

        from code_indexer.server.services.sqlite_log_handler import (
            SQLiteLogHandler,
        )

        db_path = tmp_path / "test_emit_no_commit.db"
        handler = SQLiteLogHandler(db_path)

        # Build a wrapped connection that tracks commit() calls
        real_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        wrapped_conn = MagicMock(wraps=real_conn)

        mock_manager = MagicMock()
        mock_manager.get_connection.return_value = wrapped_conn

        logger_inst = logging.getLogger("test_no_commit")
        record = logger_inst.makeRecord(
            name="test_no_commit",
            level=logging.INFO,
            fn="test_file.py",
            lno=1,
            msg="No-commit test message",
            args=(),
            exc_info=None,
        )

        with patch(
            "code_indexer.server.services.sqlite_log_handler."
            "DatabaseConnectionManager.get_instance",
            return_value=mock_manager,
        ):
            handler.emit(record)

        real_conn.close()
        handler.close()

        commit_count = wrapped_conn.commit.call_count
        assert commit_count == 0, (
            f"emit() called conn.commit() directly {commit_count} time(s); "
            "it must use execute_atomic() instead"
        )


# 5 s is generous: a non-deadlocking emit completes in <100 ms on any CI machine.
DEADLOCK_TIMEOUT_SECONDS = 5.0


class TestReentryDeadlockRegression:
    """
    Regression tests for Bug #731: SQLiteLogHandler recursive emit deadlock.

    Root cause: SQLiteLogHandler.emit -> DatabaseConnectionManager.get_connection
    -> _cleanup_stale_connections -> logger.info -> SQLiteLogHandler.emit (again)
    -> tries to acquire root logger lock already held -> DEADLOCK.

    Fix (Option A): thread-local re-entry guard in emit() silently drops
    recursive calls, preventing the deadlock without losing the outer log.

    All tests use real SQLiteLogHandler, real SQLite, real threads. No mocks
    of the handler itself.
    """

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _make_handler_and_manager(
        db_path: Path,
    ) -> Tuple[logging.Handler, object]:
        """Create a SQLiteLogHandler and its DatabaseConnectionManager."""
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        handler = SQLiteLogHandler(db_path)
        manager = DatabaseConnectionManager.get_instance(str(db_path))
        return handler, manager

    @staticmethod
    def _read_log_messages(db_path: Path) -> List[str]:
        """Return all `message` column values from the logs table."""
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT message FROM logs ORDER BY id").fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def _make_cleanup_that_logs(
        handler: logging.Handler, original_cleanup: Callable[[], None]
    ) -> Callable[[], None]:
        """Return a _cleanup_stale_connections replacement that emits a log."""

        def cleanup_that_logs() -> None:
            inner = logging.LogRecord(
                name="test.inner",
                level=logging.INFO,
                pathname="test_file.py",
                lineno=2,
                msg="Inner record — must be dropped silently",
                args=(),
                exc_info=None,
            )
            handler.emit(inner)
            original_cleanup()

        return cleanup_that_logs

    # ------------------------------------------------------------------- tests

    def test_emit_does_not_deadlock_on_recursive_logging(self, tmp_path: Path) -> None:
        """
        Bug #731 regression: emit() must not deadlock when a secondary log call
        fires during its own execution (simulating logger.info inside
        _cleanup_stale_connections).  A deadlock causes the worker thread to
        never set test_completed, failing the timeout assertion.
        """
        from unittest.mock import patch

        handler, manager = self._make_handler_and_manager(tmp_path / "deadlock_test.db")
        recursive_attempted = threading.Event()
        test_completed = threading.Event()
        original_cleanup = manager._cleanup_stale_connections  # type: ignore[union-attr]

        def patched_cleanup() -> None:
            recursive_attempted.set()
            logging.getLogger("cidx.db").info("Simulated recursive cleanup log")
            original_cleanup()

        outer = logging.LogRecord(
            name="test.deadlock",
            level=logging.INFO,
            pathname="test_file.py",
            lineno=1,
            msg="Outer log — must not deadlock",
            args=(),
            exc_info=None,
        )
        root_logger = logging.getLogger()

        def run_emit() -> None:
            try:
                handler.emit(outer)
            finally:
                test_completed.set()

        with (
            patch.object(manager, "_cleanup_stale_connections", patched_cleanup),
            patch.object(type(manager), "_last_global_cleanup", 0.0),
            patch.object(root_logger, "handlers", [handler]),
            patch.object(root_logger, "level", logging.DEBUG),
        ):
            worker = threading.Thread(target=run_emit, daemon=True)
            worker.start()
            completed = test_completed.wait(timeout=DEADLOCK_TIMEOUT_SECONDS)

        handler.close()

        assert recursive_attempted.is_set(), (
            "Patched cleanup was never called — test setup error"
        )
        assert completed, (
            f"emit() timed out after {DEADLOCK_TIMEOUT_SECONDS}s — "
            "DEADLOCK DETECTED (Bug #731)"
        )

    def test_recursive_emit_is_silently_dropped_not_raised(
        self, tmp_path: Path
    ) -> None:
        """
        The re-entry guard must silently drop the inner emit() and let the outer
        emit() complete, persisting only the outer record to the database.
        """
        from unittest.mock import patch

        db_path = tmp_path / "reentry_drop_test.db"
        handler, manager = self._make_handler_and_manager(db_path)
        original_cleanup = manager._cleanup_stale_connections  # type: ignore[union-attr]
        cleanup_fn = self._make_cleanup_that_logs(handler, original_cleanup)

        outer = logging.LogRecord(
            name="test.outer",
            level=logging.INFO,
            pathname="test_file.py",
            lineno=1,
            msg="Outer record — must be persisted",
            args=(),
            exc_info=None,
        )

        with (
            patch.object(manager, "_cleanup_stale_connections", cleanup_fn),
            patch.object(type(manager), "_last_global_cleanup", 0.0),
        ):
            handler.emit(outer)

        handler.close()

        messages = self._read_log_messages(db_path)
        assert any("Outer record" in m for m in messages), (
            f"Outer record was not persisted; DB contains: {messages}"
        )
        assert not any("Inner record" in m for m in messages), (
            f"Inner (recursive) record must be dropped by re-entry guard; "
            f"DB contains: {messages}"
        )


# ---------------------------------------------------------------------------
# Part D: Real lock-contention deadlock reproduction (Codex review finding)
# ---------------------------------------------------------------------------

# Generous timeout: a non-deadlocking operation completes in <100 ms on CI.
LOCK_DEADLOCK_TIMEOUT_SECONDS = 5.0

# Time allowed for the daemon thread to signal it has acquired the outer lock.
LOCK_ACQUIRE_SIGNAL_TIMEOUT_SECONDS = 1.0

# Sentinel thread ID guaranteed to be dead: far above any realistic OS TID.
FAKE_STALE_THREAD_ID = 999_999_999


def _does_lock_deadlock_on_reentry(
    lock: Union[threading.Lock, threading.RLock],  # type: ignore[type-arg]
) -> bool:
    """
    Return True if the lock deadlocks when the same thread tries to re-acquire
    it while already holding it.

    Both threading.Lock and threading.RLock are accepted: they share the
    context-manager protocol (__enter__/__exit__) at runtime even though the
    type stubs treat them as separate types.

    Raises AssertionError if the worker thread never acquired the outer lock
    (indicates a test-setup problem rather than a re-entry deadlock).

    A daemon thread is used so the test process is never permanently blocked.
    """
    acquired_outer = threading.Event()
    completed = threading.Event()

    def _run() -> None:
        with lock:  # type: ignore[union-attr]
            acquired_outer.set()
            # Same thread tries to re-acquire.
            # Plain Lock: blocks forever (deadlock detected by timeout).
            # RLock: succeeds immediately (re-entrant by design).
            with lock:  # type: ignore[union-attr]
                pass
        completed.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    outer_acquired = acquired_outer.wait(timeout=LOCK_ACQUIRE_SIGNAL_TIMEOUT_SECONDS)
    assert outer_acquired, (
        "Worker thread failed to acquire the outer lock within "
        f"{LOCK_ACQUIRE_SIGNAL_TIMEOUT_SECONDS}s — test-setup failure, "
        "not a re-entry deadlock."
    )
    return not completed.wait(timeout=LOCK_DEADLOCK_TIMEOUT_SECONDS)


class TestLockReentryBaseline:
    """
    Baseline tests proving that plain threading.Lock deadlocks on same-thread
    re-acquisition while threading.RLock does not.

    These tests underpin the primary Bug #731 fix: changing
    DatabaseConnectionManager._lock from Lock to RLock.
    """

    def test_plain_lock_deadlocks_on_same_thread_reentry(self) -> None:
        """
        Plain threading.Lock is NOT re-entrant.  A thread holding it that tries
        to acquire it again blocks forever (detected by the watchdog timeout).
        Validates the deadlock hazard that the RLock fix resolves.
        """
        plain_lock = threading.Lock()
        assert _does_lock_deadlock_on_reentry(plain_lock), (
            "Expected plain threading.Lock to deadlock on same-thread "
            "re-acquisition but it completed — test environment anomaly."
        )

    def test_rlock_allows_same_thread_reentry(self) -> None:
        """
        threading.RLock IS re-entrant.  Same-thread re-acquisition succeeds
        immediately without blocking.  Validates RLock as the correct fix.
        """
        rlock = threading.RLock()
        assert not _does_lock_deadlock_on_reentry(rlock), (
            "threading.RLock should allow same-thread re-acquisition without "
            "deadlock but it timed out — unexpected behaviour."
        )


class TestDatabaseManagerRLockFix:
    """
    Regression tests proving that DatabaseConnectionManager uses threading.RLock
    (the primary Bug #731 fix) and that the cleanup-logging path does not deadlock.
    """

    def test_database_manager_uses_rlock(self, tmp_path: Path) -> None:
        """
        Production invariant: DatabaseConnectionManager._lock must be RLock.
        Fails immediately if someone reverts the fix to threading.Lock.
        """
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        manager = DatabaseConnectionManager.get_instance(
            str(tmp_path / "rlock_check.db")
        )
        rlock_type = type(threading.RLock())
        assert isinstance(manager._lock, rlock_type), (
            f"DatabaseConnectionManager._lock must be threading.RLock, "
            f"got {type(manager._lock).__name__}. "
            "Reverting to plain Lock reintroduces the Bug #731 deadlock."
        )

    def test_cleanup_logging_does_not_deadlock_with_rlock(self, tmp_path: Path) -> None:
        """
        Full integration: _cleanup_stale_connections() holds self._lock and logs
        via logger.info.  With SQLiteLogHandler at root and no prior thread-local
        connection in the cleanup thread, a plain Lock would deadlock; RLock must
        complete within LOCK_DEADLOCK_TIMEOUT_SECONDS.

        Setup: install SQLiteLogHandler at root, inject FAKE_STALE_THREAD_ID into
        _connections, call _cleanup_stale_connections() from a fresh thread (no
        prior _local.connection).
        """
        from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
        from code_indexer.server.storage.database_manager import (
            DatabaseConnectionManager,
        )

        db_path = tmp_path / "cleanup_deadlock_test.db"
        handler = SQLiteLogHandler(db_path)
        manager = DatabaseConnectionManager.get_instance(str(db_path))

        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level

        completed = threading.Event()
        error_holder: List[Exception] = []

        try:
            root_logger.setLevel(logging.DEBUG)
            root_logger.handlers = [handler]

            # Ensure manager has a real connection (main thread), then inject a
            # fake stale TID so _cleanup_stale_connections emits logger.info.
            manager.get_connection()
            manager._connections[FAKE_STALE_THREAD_ID] = manager.get_connection()

            def run_cleanup_on_fresh_thread() -> None:
                """
                Fresh thread: no _local.connection yet.  If _lock is plain Lock,
                the re-entry from emit() -> get_connection() -> `with self._lock:`
                deadlocks.  With RLock it completes immediately.
                """
                try:
                    manager._cleanup_stale_connections()
                except Exception as exc:
                    error_holder.append(exc)
                finally:
                    completed.set()

            worker = threading.Thread(target=run_cleanup_on_fresh_thread, daemon=True)
            worker.start()
            finished = completed.wait(timeout=LOCK_DEADLOCK_TIMEOUT_SECONDS)
        finally:
            root_logger.handlers = original_handlers
            root_logger.level = original_level
            handler.close()

        assert finished, (
            f"_cleanup_stale_connections() timed out after "
            f"{LOCK_DEADLOCK_TIMEOUT_SECONDS}s — DEADLOCK DETECTED. "
            "DatabaseConnectionManager._lock must be threading.RLock "
            "(Bug #731 primary fix)."
        )
        assert not error_holder, (
            f"Unexpected exception in cleanup thread: {error_holder}"
        )
