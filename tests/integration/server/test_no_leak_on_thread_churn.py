"""
Bug #878 Phase 6 — integration test: SQLite FD leak is closed under thread churn.

End-to-end proof, with NO mocks, that the combination of Fix A.1 (close-on-clobber),
Fix A.2 (wall-clock cleanup daemon) and Fix A.3 (explicit close_thread_connection()
in the job finally-block) actually bounds the live connection set -- and by
extension the open SQLite file descriptor count -- under heavy short-lived
thread churn.

Exercises real threads, real DatabaseConnectionManager, real SQLite, and
(optionally) the real process's open-file count via psutil.

Scenarios
---------
1. test_thread_churn_200_threads_drains_via_daemon
   - Fire 200 short-lived threads that open a connection and exit without
     explicit cleanup. Wait for the wall-clock daemon to sweep. Assert
     len(_connections) is bounded relative to threading.active_count().
2. test_thread_churn_with_job_finally_shortcuts_daemon_work
   - Same churn, but each thread body explicitly calls
     close_thread_connection() in its own finally. The daemon is still
     running but should find nothing to do. Assert _connections drains
     without waiting for the daemon interval.
3. test_fd_count_bounded_under_churn (OPTIONAL, psutil-gated)
   - Run 500 iterations and assert the process's open-file count for the
     SQLite db path stays within a tight band of the baseline.

All three scenarios are marked @pytest.mark.integration via the module-level
pytestmark so they do not run in the fast-automation gate but are picked up
by full-automation / server-fast-automation when integration tests are
selected.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


# Apply @pytest.mark.integration to every test in this module.
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Named constants (no magic numbers in test bodies).
# ---------------------------------------------------------------------------

# Number of short-lived threads to spawn in the primary churn scenarios. Large
# enough to expose any race in Fix A.1 / A.2 / A.3, small enough to finish
# inside a normal test budget.
THREAD_CHURN_COUNT: int = 200

# Daemon sweep cadence used by the integration tests. Aggressive enough that
# a few hundred milliseconds of real wall-clock waiting exercises multiple
# sweeps, lazy enough that the daemon isn't a CPU hog.
DAEMON_SWEEP_INTERVAL_SECONDS: float = 0.1

# Max wall-clock seconds we'll wait for the daemon to drain _connections
# after the last churn thread has exited.
DAEMON_DRAIN_TIMEOUT_SECONDS: float = 3.0

# Polling cadence while waiting on a daemon-driven condition.
POLL_INTERVAL_SECONDS: float = 0.05

# Timeout we pass to stop_cleanup_daemon(). The daemon uses event.wait() and
# must unblock well within this budget.
DAEMON_STOP_TIMEOUT_SECONDS: float = 2.0

# Tolerance slack on top of threading.active_count() when asserting the upper
# bound on len(_connections) after a daemon drain. Covers the pytest worker
# thread, any fixture / harness threads, and minor timing jitter between the
# daemon's sweep and our assertion.
ACTIVE_THREAD_TOLERANCE: int = 5

# Per-thread timeout when joining churn workers. A hung worker here would
# hide the real bug, so we keep this tight.
THREAD_JOIN_TIMEOUT_SECONDS: float = 5.0

# FD-test specific: how many churn iterations to run when psutil is available.
FD_TEST_ITERATIONS: int = 500

# Tolerance for FD count drift relative to baseline. Anything inside this band
# counts as "bounded"; anything outside means we're leaking FDs.
FD_DRIFT_TOLERANCE: int = 50


# ---------------------------------------------------------------------------
# Shared helper: reset class-level state for cross-test isolation.
# ---------------------------------------------------------------------------


def _reset_manager_class_state() -> None:
    """
    Canonical reset sequence for DatabaseConnectionManager class-level state.

    Ordered steps (order matters):
      1. Stop the wall-clock cleanup daemon.  Safe no-op if no daemon is
         running.  Must come first so a sweep mid-teardown does not race the
         connection closes below.
      2. Explicitly close every tracked connection on every registered
         instance by calling close_all(), which iterates the per-instance
         _connections map and closes each sqlite3.Connection.  Doing this
         BEFORE clearing the _instances registry guarantees we do not drop
         the only reference to a live connection -- which would itself leak
         an FD and defeat the purpose of this very file.  close_all() also
         pops each instance from the registry, so the snapshot we iterate
         mutates the source; we copy values() into a list first.
      3. Clear any surviving registry entries as a defensive backstop
         (close_all()-raising instances could leave themselves registered).
      4. Reset _last_global_cleanup telemetry so throttle-sensitive asserts
         in other tests don't inherit our timestamp.
    """
    # 1. Stop daemon first.
    DatabaseConnectionManager.stop_cleanup_daemon(timeout=DAEMON_STOP_TIMEOUT_SECONDS)

    # 2. Close every tracked connection on every registered instance.  Copy
    #    values() into a list because close_all() deregisters from the
    #    singleton registry during iteration.
    for instance in list(DatabaseConnectionManager._instances.values()):
        try:
            instance.close_all()
        except Exception as exc:
            # A broken close_all() on one instance must not prevent the
            # others from being cleaned up.  Log and continue; any surviving
            # registry entry is cleared by step 3.
            logger.warning(
                "Failed to close_all() on DatabaseConnectionManager "
                "instance for %s during test teardown: %s",
                instance.db_path,
                exc,
            )

    # 3. Defensive backstop clear: close_all() normally pops each instance,
    #    but a raising instance might linger.
    DatabaseConnectionManager._instances.clear()

    # 4. Telemetry reset.
    DatabaseConnectionManager._last_global_cleanup = 0.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Absolute path to an empty SQLite db file for one test."""
    db = tmp_path / "thread_churn.db"
    db.touch()
    return str(db)


@pytest.fixture(autouse=True)
def isolated_manager_registry_and_daemon():
    """
    Reset class-level DatabaseConnectionManager state before and after each
    test via the shared _reset_manager_class_state() helper.

    Pre-yield reset guarantees no daemon / instance leakage from prior tests
    can contaminate our measurements.  Post-yield reset mirrors the same
    sequence so we don't leave a live background thread or a pile of open
    FDs behind when a test fails mid-assertion.
    """
    _reset_manager_class_state()
    yield
    _reset_manager_class_state()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_until(
    predicate, timeout: float, interval: float = POLL_INTERVAL_SECONDS
) -> bool:
    """
    Poll `predicate()` until it returns truthy or `timeout` seconds elapse.

    Returns the final truthy result (or the last falsy result if the timeout
    expired). Used to wait on daemon-driven state changes without sleeping
    the full budget when the condition clears quickly.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(interval)


def _run_churn_threads(
    mgr: DatabaseConnectionManager,
    count: int,
    explicit_close: bool,
) -> None:
    """
    Spawn `count` short-lived threads that each touch `mgr.get_connection()`.

    If `explicit_close` is True, each thread calls
    `mgr.close_thread_connection()` in a finally block (simulating the
    Fix A.3 behaviour in BackgroundJobManager._execute_job).

    Blocks until every spawned thread has exited, so the caller can safely
    assert on _connections once this returns.
    """

    def worker() -> None:
        try:
            conn = mgr.get_connection()
            # Prove the connection is live. Cursor is closed before the
            # thread exits so only the connection FD itself is in play.
            cur = conn.cursor()
            try:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                assert row is not None and row[0] == 1
            finally:
                cur.close()
        finally:
            if explicit_close:
                mgr.close_thread_connection()

    threads: List[threading.Thread] = []
    for _ in range(count):
        t = threading.Thread(target=worker, daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        # A still-alive worker here would mean the test harness itself is
        # broken (e.g. SQLite hung on fork). Bail out immediately so we
        # don't mask the real bug with a later assertion.
        assert not t.is_alive(), (
            f"Churn worker thread did not exit within {THREAD_JOIN_TIMEOUT_SECONDS}s"
        )


# ---------------------------------------------------------------------------
# Scenario 1: daemon-driven drain
# ---------------------------------------------------------------------------


def test_thread_churn_200_threads_drains_via_daemon(db_path: str) -> None:
    """
    Fire 200 short-lived threads that open a connection and exit WITHOUT
    explicit cleanup. The wall-clock cleanup daemon (Fix A.2) must sweep
    the stale TID entries so len(_connections) stays bounded.
    """
    mgr = DatabaseConnectionManager.get_instance(db_path)
    DatabaseConnectionManager.start_cleanup_daemon(
        interval=DAEMON_SWEEP_INTERVAL_SECONDS
    )

    try:
        _run_churn_threads(mgr, THREAD_CHURN_COUNT, explicit_close=False)

        # Allow the daemon a bounded window to sweep. The daemon calls
        # _cleanup_stale_connections() which removes every TID that is not
        # in threading.enumerate(). After every churn thread has joined,
        # every one of their TIDs is stale.
        def drained() -> bool:
            bound = threading.active_count() + ACTIVE_THREAD_TOLERANCE
            return len(mgr._connections) <= bound

        assert _wait_until(drained, timeout=DAEMON_DRAIN_TIMEOUT_SECONDS), (
            f"Daemon failed to drain _connections within "
            f"{DAEMON_DRAIN_TIMEOUT_SECONDS}s: "
            f"len(_connections)={len(mgr._connections)}, "
            f"active_count={threading.active_count()}"
        )
    finally:
        DatabaseConnectionManager.stop_cleanup_daemon(
            timeout=DAEMON_STOP_TIMEOUT_SECONDS
        )


# ---------------------------------------------------------------------------
# Scenario 2: explicit finally-block cleanup short-circuits the daemon
# ---------------------------------------------------------------------------


def test_thread_churn_with_job_finally_shortcuts_daemon_work(db_path: str) -> None:
    """
    Same churn, but each worker explicitly calls close_thread_connection()
    in its own finally block (Fix A.3's behaviour). The daemon is still
    running, but _connections should drain immediately when the last
    worker exits -- no daemon sweep required.

    The strict assertion (<=1) accounts for the possibility that the
    main test thread itself has an entry in _connections (it hasn't
    called get_connection() in this test, so in practice we expect
    exactly 0; 1 is the safety margin for test-harness / fixture
    threads that may briefly touch the manager).
    """
    mgr = DatabaseConnectionManager.get_instance(db_path)
    DatabaseConnectionManager.start_cleanup_daemon(
        interval=DAEMON_SWEEP_INTERVAL_SECONDS
    )

    try:
        _run_churn_threads(mgr, THREAD_CHURN_COUNT, explicit_close=True)

        # No wait_until loop: Fix A.3 guarantees each worker has already
        # popped itself out of _connections before thread exit. Any lingering
        # entries would be an explicit regression.
        assert len(mgr._connections) <= 1, (
            f"Expected _connections to drain via explicit finally cleanup, "
            f"got len(_connections)={len(mgr._connections)}: "
            f"{list(mgr._connections.keys())}"
        )
    finally:
        DatabaseConnectionManager.stop_cleanup_daemon(
            timeout=DAEMON_STOP_TIMEOUT_SECONDS
        )


# ---------------------------------------------------------------------------
# Scenario 3: OPTIONAL -- process-level open-file count remains bounded
# ---------------------------------------------------------------------------


def _count_open_fds_for_path(proc, path: str) -> int:
    """
    Return the number of open-file entries on `proc` whose path matches
    `path`. Tolerates the mid-iteration process-state churn psutil sometimes
    throws by catching per-call exceptions and treating them as 0 for that
    entry.
    """
    import psutil  # local import: only reached on the psutil-gated path

    target = os.path.abspath(path)
    count = 0
    try:
        open_files = proc.open_files()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return 0

    for f in open_files:
        try:
            if os.path.abspath(f.path) == target:
                count += 1
        except Exception:
            # A path we can't resolve is not our SQLite file; skip.
            continue
    return count


def test_fd_count_bounded_under_churn(db_path: str) -> None:
    """
    OPTIONAL: Under 500 iterations of thread churn, the process's open-file
    count for the SQLite db path must stay within a tight band of the
    baseline. Requires psutil; skipped if psutil is unavailable.
    """
    try:
        import psutil  # noqa: F401
    except ImportError:
        pytest.skip("psutil required for FD count assertion")

    import psutil as _psutil  # re-import inside the test for type clarity

    proc = _psutil.Process(os.getpid())
    mgr = DatabaseConnectionManager.get_instance(db_path)
    DatabaseConnectionManager.start_cleanup_daemon(
        interval=DAEMON_SWEEP_INTERVAL_SECONDS
    )

    try:
        # Baseline: before any churn, how many FDs already point at this db?
        # Usually 0, but other tests / fixtures could have pinned one.
        baseline = _count_open_fds_for_path(proc, db_path)

        _run_churn_threads(mgr, FD_TEST_ITERATIONS, explicit_close=False)

        # Allow the daemon time to sweep. Under Fix A.1+A.2 the peak FD
        # count during churn may briefly exceed baseline+tolerance, but
        # after the daemon drains it must be well within the band.
        def fd_within_band() -> bool:
            current = _count_open_fds_for_path(proc, db_path)
            return current <= baseline + FD_DRIFT_TOLERANCE

        assert _wait_until(fd_within_band, timeout=DAEMON_DRAIN_TIMEOUT_SECONDS), (
            f"Open-file count for {db_path} exceeds baseline "
            f"({baseline}) by more than {FD_DRIFT_TOLERANCE}: "
            f"current={_count_open_fds_for_path(proc, db_path)}"
        )
    finally:
        DatabaseConnectionManager.stop_cleanup_daemon(
            timeout=DAEMON_STOP_TIMEOUT_SECONDS
        )
