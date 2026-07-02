"""
Tests for DatabaseConnectionManager wall-clock cleanup daemon (Bug #878 Fix A.2).

Root Cause RC-3: Cleanup was piggybacked on get_connection() traffic. In production,
short-lived background-job threads churn faster than sweep cadence, and cleanup gaps
of 1-16 minutes were observed when get_connection() traffic was thin. The fix is a
dedicated background daemon thread that wakes on wall-clock cadence and sweeps all
registered DatabaseConnectionManager instances, regardless of get_connection() traffic.

These tests exercise the new classmethods:
- DatabaseConnectionManager.start_cleanup_daemon(interval)
- DatabaseConnectionManager.stop_cleanup_daemon(timeout)
- DatabaseConnectionManager._cleanup_daemon_loop(interval)   (internal)

They use real threads and real SQLite connections, with no mocks of the system
under test. The exception-survival test plugs a real subclass instance whose
_cleanup_stale_connections() raises on the first invocation; this simulates a
genuine sweep failure at an instance boundary without patching core classmethods.

All resource acquisition is wrapped in try/finally so the daemon thread and
SQLite connections are released deterministically regardless of assertion
outcomes.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager


# ---------------------------------------------------------------------------
# Named constants (no magic numbers in test bodies)
# ---------------------------------------------------------------------------

# Sentinel thread id we inject into _connections to simulate a dead short-lived
# worker thread whose OS TID is no longer in threading.enumerate().
FAKE_DEAD_THREAD_ID: int = 99999999

# Daemon sweep cadence used by fast-running tests. Small enough that ~1s of
# wall-clock test wait exercises multiple sweeps, large enough to avoid a
# busy loop.
FAST_SWEEP_INTERVAL_SECONDS: float = 0.1

# Sweep interval for the stop-terminates-cleanly test. We want the daemon to
# be WAITING on the stop event (not mid-sweep) when we signal stop.
MEDIUM_SWEEP_INTERVAL_SECONDS: float = 0.5

# Timeout we pass to stop_cleanup_daemon(). The daemon uses event.wait() and
# must unblock well within this budget.
STOP_TIMEOUT_SECONDS: float = 2.0

# Overall wall-clock deadline a test will wait for a daemon-driven condition
# to become true (stale entry removal, second sweep after exception, etc.).
WAIT_TIMEOUT_SECONDS: float = 1.0

# Polling cadence used to re-check test assertions while waiting for the
# daemon to act. Short enough to keep total test time tight.
POLL_INTERVAL_SECONDS: float = 0.05


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    """Yield an absolute path to an empty SQLite DB file."""
    db = tmp_path / "test_cleanup_daemon.db"
    db.touch()
    yield str(db)


@pytest.fixture(autouse=True)
def isolated_manager_registry():
    """
    Reset class-level state before/after each test and ensure the daemon is
    stopped cleanly even if a test fails mid-way.
    """
    # Pre-test reset. The daemon classmethods may not yet exist during the
    # RED phase; guard only for AttributeError in that specific case.
    try:
        DatabaseConnectionManager.stop_cleanup_daemon(timeout=STOP_TIMEOUT_SECONDS)
    except AttributeError:
        pass
    DatabaseConnectionManager._instances.clear()
    DatabaseConnectionManager._last_global_cleanup = 0.0

    yield

    # Post-test teardown: mirror of pre-test reset.
    try:
        DatabaseConnectionManager.stop_cleanup_daemon(timeout=STOP_TIMEOUT_SECONDS)
    except AttributeError:
        pass
    DatabaseConnectionManager._instances.clear()
    DatabaseConnectionManager._last_global_cleanup = 0.0


# ---------------------------------------------------------------------------
# Real subclass used to exercise the exception-survival path without
# monkeypatching any SUT classmethod. This is a genuine dependency boundary:
# _cleanup_all_instances() iterates over registered instances and calls
# inst._cleanup_stale_connections() on each. Swapping in a subclass whose
# instance-level cleanup raises on the first call is a real failure, not a
# mock.
# ---------------------------------------------------------------------------


class _FlakySweepManager(DatabaseConnectionManager):
    """
    DatabaseConnectionManager variant whose first _cleanup_stale_connections()
    invocation raises. Used to verify the daemon survives a real sweep failure
    at the instance boundary and proceeds to invoke the next tick.
    """

    def __init__(self, db_path: str, second_call_event: threading.Event) -> None:
        super().__init__(db_path)
        self._call_count: int = 0
        self._second_call_event = second_call_event

    def _cleanup_stale_connections(self) -> None:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError(
                "simulated instance-level sweep failure (daemon must survive)"
            )
        # Second or later sweep: signal the waiter and run the real cleanup.
        self._second_call_event.set()
        super()._cleanup_stale_connections()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until_absent(
    mgr: DatabaseConnectionManager, tid: int, deadline: float
) -> bool:
    """Poll until `tid` is absent from mgr._connections or deadline elapses."""
    while time.time() < deadline:
        with mgr._lock:
            if tid not in mgr._connections:
                return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def _is_connection_open(conn: sqlite3.Connection) -> bool:
    """
    Return True iff `conn` can execute a trivial query (i.e. not closed).
    We use this to deterministically decide whether a connection still needs
    closing during teardown, avoiding a bare try/except.
    """
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        # sqlite3 raises ProgrammingError specifically on use-after-close;
        # that is the only expected outcome here and is treated as "closed".
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestCleanupDaemon:
    """Tests for the wall-clock cleanup daemon (Bug #878 Fix A.2)."""

    @pytest.mark.slow
    def test_daemon_cleans_stale_without_get_connection_traffic(self, tmp_db):
        """
        The daemon must sweep stale connections on wall-clock cadence, even when
        no get_connection() call happens.

        Setup:
        - Create a manager, open a real connection on the main thread
        - Inject a fake entry at a thread_id that is NOT in threading.enumerate()

        Expected:
        - Within WAIT_TIMEOUT_SECONDS, the daemon's periodic sweep removes the
          fake entry from _connections, WITHOUT the main thread calling
          get_connection().
        """
        mgr = DatabaseConnectionManager(tmp_db)
        fake_conn: sqlite3.Connection = sqlite3.connect(tmp_db, check_same_thread=False)
        daemon_started: bool = False
        try:
            # Real connection on main thread -> registers in _connections[main_tid]
            mgr.get_connection()

            # Sanity: FAKE_DEAD_THREAD_ID must not collide with any live thread.
            alive = {t.ident for t in threading.enumerate()}
            assert FAKE_DEAD_THREAD_ID not in alive, (
                "FAKE_DEAD_THREAD_ID collided with a real thread id"
            )

            with mgr._lock:
                mgr._connections[FAKE_DEAD_THREAD_ID] = fake_conn

            # Register the instance in the class registry so the daemon sweeps it.
            DatabaseConnectionManager._instances[os.path.abspath(tmp_db)] = mgr

            DatabaseConnectionManager.start_cleanup_daemon(
                interval=FAST_SWEEP_INTERVAL_SECONDS
            )
            daemon_started = True

            removed = _wait_until_absent(
                mgr,
                FAKE_DEAD_THREAD_ID,
                deadline=time.time() + WAIT_TIMEOUT_SECONDS,
            )

            assert removed, (
                "Daemon did not clean up the stale fake entry within "
                f"{WAIT_TIMEOUT_SECONDS}s (wall-clock sweep failed)"
            )
        finally:
            if daemon_started:
                DatabaseConnectionManager.stop_cleanup_daemon(
                    timeout=STOP_TIMEOUT_SECONDS
                )
            # Deterministic teardown: close fake_conn only if still open.
            # _cleanup_stale_connections() closes stale connections it removes,
            # so after a successful sweep fake_conn is already closed.
            if _is_connection_open(fake_conn):
                fake_conn.close()
            mgr.close_all()

    def test_daemon_idempotent_start(self, tmp_db):
        """
        Calling start_cleanup_daemon twice should NOT spawn a second daemon
        thread. The second call must be a no-op (idempotent).
        """
        daemon_started: bool = False
        try:
            DatabaseConnectionManager.start_cleanup_daemon(
                interval=FAST_SWEEP_INTERVAL_SECONDS
            )
            daemon_started = True
            first_thread = DatabaseConnectionManager._cleanup_thread
            assert first_thread is not None
            assert first_thread.is_alive()

            # Second call must not replace the running thread
            DatabaseConnectionManager.start_cleanup_daemon(
                interval=FAST_SWEEP_INTERVAL_SECONDS
            )
            second_thread = DatabaseConnectionManager._cleanup_thread
            assert second_thread is first_thread, (
                "Second start call should be a no-op and not replace the "
                "running daemon thread"
            )
            assert second_thread.is_alive()
        finally:
            if daemon_started:
                DatabaseConnectionManager.stop_cleanup_daemon(
                    timeout=STOP_TIMEOUT_SECONDS
                )
        assert DatabaseConnectionManager._cleanup_thread is None

    def test_daemon_stop_terminates_cleanly_within_timeout(self, tmp_db):
        """
        stop_cleanup_daemon(timeout=STOP_TIMEOUT_SECONDS) must join the daemon
        thread cleanly and leave it no longer alive.
        """
        thread_ref: threading.Thread | None = None
        daemon_started: bool = False
        try:
            DatabaseConnectionManager.start_cleanup_daemon(
                interval=MEDIUM_SWEEP_INTERVAL_SECONDS
            )
            daemon_started = True
            thread_ref = DatabaseConnectionManager._cleanup_thread
            assert thread_ref is not None
            assert thread_ref.is_alive()

            DatabaseConnectionManager.stop_cleanup_daemon(timeout=STOP_TIMEOUT_SECONDS)
            daemon_started = False  # stop_cleanup_daemon fully shut it down

            assert not thread_ref.is_alive(), "Daemon thread should no longer be alive"
            assert DatabaseConnectionManager._cleanup_thread is None
            assert DatabaseConnectionManager._cleanup_stop_event is None
        finally:
            if daemon_started:
                DatabaseConnectionManager.stop_cleanup_daemon(
                    timeout=STOP_TIMEOUT_SECONDS
                )

    def test_daemon_survives_exception_in_sweep(self, tmp_db):
        """
        If a registered instance's _cleanup_stale_connections() raises on a
        given tick, the daemon must log and continue, invoking the sweep
        again on the next tick.

        Setup:
        - Register a real _FlakySweepManager (subclass) instance; its first
          _cleanup_stale_connections() call raises, subsequent calls set an
          event.
        - Start the daemon at a fast cadence.

        Expected:
        - The event is set within WAIT_TIMEOUT_SECONDS, proving the daemon
          performed a second sweep after the first raised -- i.e. it did NOT
          die on the exception.
        """
        second_call_event = threading.Event()
        flaky_mgr = _FlakySweepManager(tmp_db, second_call_event)
        DatabaseConnectionManager._instances[os.path.abspath(tmp_db)] = flaky_mgr
        daemon_started: bool = False
        try:
            DatabaseConnectionManager.start_cleanup_daemon(
                interval=FAST_SWEEP_INTERVAL_SECONDS
            )
            daemon_started = True

            fired = second_call_event.wait(timeout=WAIT_TIMEOUT_SECONDS)

            assert fired, (
                "Daemon did not run a second sweep after the first raised -- "
                "daemon died on exception"
            )
            assert flaky_mgr._call_count >= 2, (
                f"Expected >=2 instance sweep invocations, got {flaky_mgr._call_count}"
            )
        finally:
            if daemon_started:
                DatabaseConnectionManager.stop_cleanup_daemon(
                    timeout=STOP_TIMEOUT_SECONDS
                )
            flaky_mgr.close_all()
