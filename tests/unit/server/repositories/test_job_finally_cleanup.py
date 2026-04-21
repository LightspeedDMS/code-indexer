"""
Tests for BackgroundJobManager._execute_job try/finally cleanup of SQLite thread
connections (Bug #878 Fix A.3).

Root Cause RC-3: Short-lived job threads accumulate in DatabaseConnectionManager
._connections faster than the demand-driven (piggyback) cleanup sweep can drain
them. Fix A.2 (separate story) adds a wall-clock cleanup daemon, but even that
daemon discovers stale TIDs AFTER the fact. Fix A.3 closes the tightest possible
loop: when a BackgroundJob thread exits, its tracked connection(s) are closed
immediately and proactively on all registered DatabaseConnectionManager instances.

Fix A.3:
  1. New instance method DatabaseConnectionManager.close_thread_connection()
     that closes and untracks the calling thread's entry in _connections.
  2. BackgroundJobManager._execute_job is wrapped in an outer try/finally. The
     finally iterates DatabaseConnectionManager._instances.values() and calls
     close_thread_connection() on each, swallowing any exceptions so one
     broken manager cannot prevent cleanup on the others.

These tests use REAL threads, REAL SQLite, and REAL BackgroundJobManager. No
mocks for the code under test. SQLite files are temp files from tmp_path.

CRITICAL: completion is proven by join()ing the worker thread captured inside
the wrapped job function. That guarantees _execute_job (and therefore its
OUTER finally, which is where Fix A.3 cleanup lives) has returned before any
assertion is checked. We do NOT rely on _running_jobs emptying, because that
pop happens in the INNER finally which fires before the outer Fix A.3 cleanup.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


pytestmark = pytest.mark.slow


@dataclass
class _CapturedWorker:
    """Typed container for worker-thread state captured from inside the job.

    Replaces the earlier loose dict[str, object] pattern so type checkers
    can validate attribute accesses in helpers without # type: ignore.
    """

    tid: Optional[int] = None
    thread: Optional[threading.Thread] = None
    ready: threading.Event = field(default_factory=threading.Event)


@pytest.fixture(autouse=True)
def isolated_manager_registry():
    """Clear DatabaseConnectionManager singleton registry before/after test.

    Mutations of the class-level _instances dict are made while holding
    _instance_lock to avoid races with worker threads that may still be
    unwinding from an earlier test.
    """
    with DatabaseConnectionManager._instance_lock:
        DatabaseConnectionManager._instances.clear()
        DatabaseConnectionManager._last_global_cleanup = 0.0
    yield
    with DatabaseConnectionManager._instance_lock:
        DatabaseConnectionManager._instances.clear()


@pytest.fixture
def manager_factory(tmp_path: Path):
    """Yield a factory(storage_subdir) -> BackgroundJobManager.

    The yielded callable constructs a BackgroundJobManager rooted at
    tmp_path / f"{storage_subdir}.json" with a 4-slot concurrency config.
    Every manager returned is tracked internally and shutdown() is
    invoked on each during fixture teardown.
    """
    created: list[BackgroundJobManager] = []

    def _factory(storage_subdir: str = "jobs") -> BackgroundJobManager:
        storage_path = tmp_path / f"{storage_subdir}.json"
        config = BackgroundJobsConfig(max_concurrent_background_jobs=4)
        mgr = BackgroundJobManager(
            storage_path=str(storage_path),
            background_jobs_config=config,
        )
        created.append(mgr)
        return mgr

    yield _factory

    for mgr in created:
        try:
            mgr.shutdown()
        except Exception:
            logging.getLogger(__name__).warning(
                "manager_factory teardown: shutdown() raised", exc_info=True
            )


def _submit_and_wait(
    mgr: BackgroundJobManager,
    func,
    *,
    username: str = "tester",
    timeout_seconds: float = 10.0,
) -> tuple[str, int]:
    """Submit a job, join its worker thread, return (job_id, worker_tid).

    Completion is proven by retaining a reference to the worker's
    threading.Thread object and then join()ing it. Thread.join returns
    only after the target function AND every finally block on the
    worker's stack have completed, which includes Fix A.3's OUTER
    finally in _execute_job. This is the only reliable completion
    signal — we do NOT rely on _running_jobs emptying because that
    pop happens in an INNER finally that fires before Fix A.3's outer
    cleanup finishes.
    """
    captured = _CapturedWorker()

    def wrapped(**kwargs):
        captured.tid = threading.get_ident()
        captured.thread = threading.current_thread()
        captured.ready.set()
        return func(**kwargs)

    job_id = mgr.submit_job(
        operation_type="test_op",
        func=wrapped,
        submitter_username=username,
        repo_alias=f"test-repo-{threading.get_ident()}",
    )

    assert captured.ready.wait(timeout=timeout_seconds), (
        f"Job {job_id} worker did not start within {timeout_seconds}s"
    )
    worker_thread = captured.thread
    assert worker_thread is not None, "Expected to capture worker threading.Thread"

    worker_thread.join(timeout=timeout_seconds)
    if worker_thread.is_alive():
        pytest.fail(
            f"Worker thread for {job_id} did not exit within {timeout_seconds}s"
        )

    tid = captured.tid
    assert tid is not None, "Expected to capture worker TID"
    return job_id, tid


class _BrokenDatabaseConnectionManager(DatabaseConnectionManager):
    """A DatabaseConnectionManager whose close_thread_connection() always raises.

    Used to assert Fix A.3's finally block is DEFENSIVE — one broken
    manager must not prevent cleanup on sibling managers, and the
    thread must not crash. The BREAK_MESSAGE sentinel is asserted
    against caplog records in the defensive test to prove the warning
    originated from the cleanup path rather than unrelated server log
    noise.
    """

    BREAK_MESSAGE = "_BrokenDatabaseConnectionManager: close_thread_connection blew up"

    def close_thread_connection(self) -> None:
        # Signature matches the base class method introduced by Fix A.3
        # (see DatabaseConnectionManager.close_thread_connection).  No
        # type: ignore is needed because base and override both take no
        # arguments and return None.
        raise RuntimeError(self.BREAK_MESSAGE)


def _register_broken_and_good(
    tmp_path: Path,
) -> tuple[_BrokenDatabaseConnectionManager, DatabaseConnectionManager]:
    """Register a broken + good manager in the singleton dict and return them.

    The broken manager is inserted into DatabaseConnectionManager
    ._instances directly because get_instance() would build a plain
    DatabaseConnectionManager and we need the _BrokenDatabaseConnectionManager
    subclass for its raising close_thread_connection override.
    """
    import os

    broken_path = str(tmp_path / "broken.db")
    good_path = str(tmp_path / "good.db")
    broken_resolved = os.path.abspath(broken_path)

    broken_mgr = _BrokenDatabaseConnectionManager(broken_path)
    with DatabaseConnectionManager._instance_lock:
        DatabaseConnectionManager._instances[broken_resolved] = broken_mgr

    good_mgr = DatabaseConnectionManager.get_instance(good_path)
    return broken_mgr, good_mgr


def _find_cleanup_warnings(caplog) -> list[logging.LogRecord]:
    """Return caplog warning records tied to Fix A.3 defensive cleanup.

    A record qualifies if its message mentions "close_thread_connection"
    (the defensive path in _execute_job's outer finally must name the
    method that raised) OR contains the sentinel BREAK_MESSAGE emitted
    by _BrokenDatabaseConnectionManager. This makes the assertion
    specific to the cleanup path rather than accepting any warning.
    """
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and (
            "close_thread_connection" in r.getMessage()
            or _BrokenDatabaseConnectionManager.BREAK_MESSAGE in r.getMessage()
        )
    ]


def test_job_thread_connection_closed_on_success(
    tmp_path: Path, manager_factory
) -> None:
    """Fix A.3: successful job → worker TID is untracked after thread exits."""
    db_path = str(tmp_path / "cleanup_success.db")
    db_manager = DatabaseConnectionManager.get_instance(db_path)
    mgr = manager_factory()

    def job_handler():
        conn = db_manager.get_connection()
        conn.execute("SELECT 1").fetchone()
        return {"success": True}

    _job_id, worker_tid = _submit_and_wait(mgr, job_handler)

    with db_manager._lock:
        assert worker_tid not in db_manager._connections, (
            f"Fix A.3 violated: worker TID {worker_tid} still tracked in "
            f"_connections after successful job. Keys: "
            f"{list(db_manager._connections.keys())}"
        )


def test_job_thread_connection_closed_on_exception(
    tmp_path: Path, manager_factory
) -> None:
    """Fix A.3: exception in job → worker TID STILL untracked (finally fires).

    On the non-progress_callback path, the user's func runs on a nested
    worker thread INSIDE ``_execute_with_cancellation_check``.  That
    nested thread opens the SQLite connection under its own TID, and
    Fix A.3's inner-worker finally closes/untracks it before the nested
    thread exits.  The OUTER ``_execute_job`` thread then catches the
    propagated RuntimeError and sets ``job.status = FAILED`` — but it
    does so AFTER the nested worker has joined, so we must wait for the
    outer thread too before asserting on status.
    """
    import time

    from code_indexer.server.repositories.background_jobs import JobStatus

    db_path = str(tmp_path / "cleanup_exception.db")
    db_manager = DatabaseConnectionManager.get_instance(db_path)
    mgr = manager_factory()

    def failing_handler():
        conn = db_manager.get_connection()
        conn.execute("SELECT 1").fetchone()
        raise RuntimeError("test failure injected by Fix A.3 test")

    job_id, worker_tid = _submit_and_wait(mgr, failing_handler)

    # Primary Fix A.3 assertion: TID must be gone from _connections.
    # This is what Fix A.3 fixes and the reason this test exists.
    with db_manager._lock:
        assert worker_tid not in db_manager._connections, (
            f"Fix A.3 violated: worker TID {worker_tid} still tracked in "
            f"_connections after job raised RuntimeError. Keys: "
            f"{list(db_manager._connections.keys())}"
        )

    # Secondary assertion: the OUTER _execute_job thread eventually marks
    # the job FAILED in its inner except-Exception branch.  Poll with a
    # bounded timeout rather than sleeping a fixed amount; proof is that
    # _running_jobs has popped job_id (that pop is in the inner finally
    # which runs AFTER the status update).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        with mgr._lock:
            still_running = job_id in mgr._running_jobs
        if not still_running:
            break
        time.sleep(0.01)
    else:
        pytest.fail(
            f"Outer _execute_job thread for {job_id} did not complete "
            "within 5s after nested worker raised"
        )

    with mgr._lock:
        job = mgr.jobs.get(job_id)
    if job is not None:
        assert job.status == JobStatus.FAILED, (
            f"Job after RuntimeError should be FAILED, got {job.status}"
        )


def test_finally_iterates_all_registered_managers(
    tmp_path: Path, manager_factory
) -> None:
    """Fix A.3: finally iterates _instances — cleanup covers every manager."""
    db_path_a = str(tmp_path / "managerA.db")
    db_path_b = str(tmp_path / "managerB.db")
    mgr_a = DatabaseConnectionManager.get_instance(db_path_a)
    mgr_b = DatabaseConnectionManager.get_instance(db_path_b)
    assert mgr_a is not mgr_b, "Distinct paths must yield distinct instances"

    bg_mgr = manager_factory()

    def job_handler():
        mgr_a.get_connection().execute("SELECT 1").fetchone()
        mgr_b.get_connection().execute("SELECT 1").fetchone()
        return {"success": True}

    _job_id, worker_tid = _submit_and_wait(bg_mgr, job_handler)

    with mgr_a._lock:
        assert worker_tid not in mgr_a._connections, (
            f"Fix A.3 violated: TID {worker_tid} still tracked on manager A. "
            f"Keys: {list(mgr_a._connections.keys())}"
        )
    with mgr_b._lock:
        assert worker_tid not in mgr_b._connections, (
            f"Fix A.3 violated: TID {worker_tid} still tracked on manager B. "
            f"Keys: {list(mgr_b._connections.keys())}"
        )


def test_finally_is_defensive_does_not_raise(
    tmp_path: Path, manager_factory, caplog
) -> None:
    """Fix A.3: one broken manager must not break cleanup on siblings.

    Proof of defensiveness lives in three checks:
      (a) worker thread joined cleanly (enforced inside _submit_and_wait;
          if the outer finally re-raised, the thread would still exit
          but any later registered-manager cleanup would be skipped —
          check (c) is what actually proves (a) held),
      (b) a WARNING tied to close_thread_connection was emitted,
      (c) the GOOD manager's _connections no longer holds the worker TID.
    """
    broken_mgr, good_mgr = _register_broken_and_good(tmp_path)
    bg_mgr = manager_factory()

    def job_handler():
        good_mgr.get_connection().execute("SELECT 1").fetchone()
        return {"success": True}

    caplog.set_level(logging.WARNING)
    _job_id, worker_tid = _submit_and_wait(bg_mgr, job_handler)

    with good_mgr._lock:
        assert worker_tid not in good_mgr._connections, (
            f"Fix A.3 defensiveness violated: broken manager prevented "
            f"cleanup on good manager. TID {worker_tid} still tracked. "
            f"Keys on good: {list(good_mgr._connections.keys())}"
        )

    targeted = _find_cleanup_warnings(caplog)
    assert targeted, (
        "Fix A.3 defensiveness: expected at least one WARNING log that "
        "references 'close_thread_connection' or the broken manager's "
        "exception message. Did the defensive cleanup path actually run?\n"
        "All warning records:\n"
        + "\n".join(
            f"  [{r.name}] {r.getMessage()}"
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )
    )
