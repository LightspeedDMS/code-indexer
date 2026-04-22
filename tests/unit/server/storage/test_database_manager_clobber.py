"""
Tests for DatabaseConnectionManager TID-reuse close-on-clobber fix (Bug #878 Fix A.1).

Root Cause RC-2: When an OS thread ID (TID) is recycled (Linux reuses TIDs immediately),
a new BackgroundJob thread gets the same TID as a previous dead thread. The new thread's
threading.local storage is empty (new thread object), so it opens a new sqlite3.connect().
The old code then does self._connections[thread_id] = conn, overwriting the prior entry
WITHOUT closing it. The prior connection leaks silently until Python GC collects it —
and GC timing is non-deterministic, meaning FDs can accumulate faster than GC drains them.

Fix A.1: Before writing to self._connections[thread_id], check whether an existing entry
is present and is a DIFFERENT connection object. If so, close it first.
"""

import contextlib
import sqlite3
import threading

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager


@pytest.fixture
def tmp_db(tmp_path):
    """Yield an absolute path to an empty SQLite DB file; remove it after the test."""
    db = tmp_path / "test_clobber.db"
    db.touch()
    yield str(db)
    # File removed automatically when tmp_path fixture cleans up


@pytest.fixture(autouse=True)
def isolated_manager_registry():
    """Clear the singleton registry and global cleanup state before each test."""
    DatabaseConnectionManager._instances.clear()
    DatabaseConnectionManager._last_global_cleanup = 0.0
    yield
    DatabaseConnectionManager._instances.clear()


class TestCloseOnClobber:
    """Tests for close-on-clobber fix (Bug #878 Fix A.1)."""

    def test_clobber_closes_prior_connection(self, tmp_db):
        """
        Simulate TID reuse: open conn1 on thread_id=X, then inject a second connection
        at the same thread_id WITHOUT clearing threading.local (simulating clobber path
        directly). Assert conn1 was closed before conn2 is stored.

        This tests Fix A.1: the code path inside get_connection() that closes the prior
        connection at thread_id before overwriting it.
        """
        mgr = DatabaseConnectionManager(tmp_db)
        try:
            # Open a first connection on this thread
            conn1 = mgr.get_connection()

            # Simulate the clobber: manually clear the thread-local so that
            # get_connection() believes there is no connection for this thread
            # (mimicking a recycled TID where the new thread has empty threading.local),
            # but the _connections dict still has conn1 at this TID.
            mgr._local.connection = None

            # Now call get_connection() again from the same thread (same TID).
            # Before Fix A.1: conn1 is silently overwritten (leaked).
            # After Fix A.1: conn1 is closed THEN conn2 is stored.
            conn2 = mgr.get_connection()
        finally:
            mgr.close_all()

        # conn2 must be a different object
        assert conn2 is not conn1, "Expected a new connection after clearing local"

        # conn1 must now be closed — using it should raise ProgrammingError
        with _assert_closed(conn1):
            conn1.execute("SELECT 1")

    def test_no_double_close_same_connection(self, tmp_db):
        """
        If threading.local already has a valid connection that matches _connections,
        get_connection() returns it without closing.
        """
        mgr = DatabaseConnectionManager(tmp_db)
        try:
            conn1 = mgr.get_connection()
            conn2 = mgr.get_connection()
        finally:
            mgr.close_all()

        # Same object — no double-close
        assert conn1 is conn2

    def test_clobber_registers_new_connection_in_dict(self, tmp_db):
        """
        After the clobber, _connections[thread_id] must point to the new connection,
        not the old (now-closed) one.
        """
        mgr = DatabaseConnectionManager(tmp_db)
        try:
            conn1 = mgr.get_connection()
            thread_id = threading.get_ident()

            # Simulate recycled TID
            mgr._local.connection = None

            conn2 = mgr.get_connection()

            with mgr._lock:
                tracked = mgr._connections.get(thread_id)
        finally:
            mgr.close_all()

        assert tracked is conn2, "_connections must point to the new connection"
        assert tracked is not conn1, "_connections must not point to the old connection"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _assert_closed(conn: sqlite3.Connection):
    """Context manager that asserts a sqlite3.Connection is already closed.

    Usage:
        with _assert_closed(conn):
            conn.execute("SELECT 1")  # should raise ProgrammingError
    """
    try:
        yield
    except sqlite3.ProgrammingError:
        # Expected: connection was closed — test passes
        pass
    else:
        raise AssertionError(
            "Expected sqlite3.ProgrammingError (closed connection) but no exception was raised. "
            "The old connection was NOT closed before clobber."
        )
