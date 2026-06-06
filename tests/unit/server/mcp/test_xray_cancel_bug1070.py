"""Bug #1070 — Workstream A: xray cancel must terminate registered child processes.

Before the fix: `bjm.cancel_job` on an xray job (registered via JobTracker only,
NOT in `bjm.jobs`) falls to the SQLite cross-node path which marks the DB row
cancelled but never calls `_terminate_child_processes`. The Rust xray-cli child
process runs to completion. Cancellation is effectively a no-op.

After the fix: when `job_id not in self.jobs` but `job_id in self._child_processes`,
`cancel_job` resolves the owner from JobTracker for auth, then calls
`_terminate_child_processes` AND marks the JobTracker job failed/cancelled.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import BackgroundJobsConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_CONCURRENT_JOBS = 10
_OWNER_USERNAME = "alice"
_OTHER_USERNAME = "bob"
_ADMIN_USERNAME = "admin"
_OPERATION_TYPE = "xray_search"
_REPO_ALIAS = "myrepo-global"
_TERMINAL_STATUSES = ("failed", "cancelled")


# ---------------------------------------------------------------------------
# Stand-in process
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Minimal stand-in for multiprocessing.Process exposing the process API.

    Tracks whether terminate() and kill() were called. is_alive() returns True
    until terminate() or kill() is called, then returns False (graceful exit).
    """

    def __init__(self) -> None:
        self._alive = True
        self.terminate_called = False
        self.kill_called = False
        self._join_calls: List[float] = []

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called = True
        self._alive = False  # simulate graceful SIGTERM exit

    def kill(self) -> None:
        self.kill_called = True
        self._alive = False

    def join(self, timeout: float = 0) -> None:
        self._join_calls.append(timeout)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(temp_dir):
    path = str(Path(temp_dir) / "test_xray_cancel.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture
def bjm_and_tracker(db_path):
    tracker = JobTracker(db_path)
    with patch(
        "code_indexer.server.services.maintenance_service.get_maintenance_state"
    ) as mock_maint:
        mock_maint.return_value.is_maintenance_mode.return_value = False
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
            ),
            job_tracker=tracker,
        )
    yield manager, tracker
    manager.shutdown()


# ---------------------------------------------------------------------------
# Shared setup helper
# ---------------------------------------------------------------------------


def _register_xray_job_with_child(
    bjm: BackgroundJobManager,
    tracker: JobTracker,
    job_id: str,
    username: str = _OWNER_USERNAME,
    operation_type: str = _OPERATION_TYPE,
    repo_alias: str = _REPO_ALIAS,
) -> _FakeProcess:
    """Register an xray job in JobTracker only (bypassing bjm.submit_job) and
    attach a fake child process to bjm._child_processes.

    Returns the _FakeProcess so callers can assert on its state.
    """
    tracker.register_job(
        job_id=job_id,
        operation_type=operation_type,
        username=username,
        repo_alias=repo_alias,
    )
    proc = _FakeProcess()
    bjm.register_child_process(job_id, proc)
    return proc


def _assert_job_terminated_and_jt_cancelled(
    proc: _FakeProcess,
    tracker: JobTracker,
    job_id: str,
) -> None:
    """Shared assertion helper for the success-path: process terminated, JT cancelled."""
    assert proc.terminate_called, (
        "Bug #1070: cancel_job must call terminate() on the registered child process "
        "for xray jobs (not in bjm.jobs but in bjm._child_processes)"
    )
    assert not proc.is_alive(), "child process must not be alive after cancel_job"

    jt_job = tracker.get_job(job_id)
    assert jt_job is not None, "JobTracker job must still be retrievable after cancel"
    assert jt_job.status in _TERMINAL_STATUSES, (
        f"Bug #1070: JobTracker status must be in {_TERMINAL_STATUSES}, "
        f"got: {jt_job.status!r}"
    )


# ---------------------------------------------------------------------------
# Tests: success paths (owner and admin) — parameterized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canceller_username, is_admin, job_id",
    [
        (_OWNER_USERNAME, False, "xray-cancel-owner-test-1"),
        (_ADMIN_USERNAME, True, "xray-cancel-admin-test-1"),
    ],
)
def test_xray_cancel_success_path_terminates_child_process(
    bjm_and_tracker,
    canceller_username: str,
    is_admin: bool,
    job_id: str,
):
    """Bug #1070 AC: owner and admin cancel an xray job → child process terminated.

    xray jobs live in JobTracker only (not bjm.jobs). Before the fix, cancel_job
    falls to the SQLite cross-node path which marks the DB row but never calls
    _terminate_child_processes, so the Rust child runs to completion.
    """
    bjm, tracker = bjm_and_tracker

    proc = _register_xray_job_with_child(bjm, tracker, job_id, username=_OWNER_USERNAME)

    # Confirm job is NOT in bjm.jobs (xray bypasses submit_job — must hold)
    with bjm._lock:
        assert job_id not in bjm.jobs, "xray jobs must not be in bjm.jobs"

    with bjm._child_processes_lock:
        assert job_id in bjm._child_processes

    assert proc.is_alive()

    result = bjm.cancel_job(job_id, canceller_username, is_admin=is_admin)

    assert result["success"] is True, (
        f"Bug #1070: cancel_job must return success=True for canceller={canceller_username!r} "
        f"is_admin={is_admin}, got: {result}"
    )
    _assert_job_terminated_and_jt_cancelled(proc, tracker, job_id)


# ---------------------------------------------------------------------------
# Test: wrong non-admin username → rejected, process not killed
# ---------------------------------------------------------------------------


def test_xray_cancel_wrong_user_rejected_process_not_killed(bjm_and_tracker):
    """Non-admin 'bob' cannot cancel 'alice's xray job; process remains alive."""
    bjm, tracker = bjm_and_tracker
    job_id = "xray-cancel-wrong-user-test-1"

    proc = _register_xray_job_with_child(bjm, tracker, job_id, username=_OWNER_USERNAME)

    result = bjm.cancel_job(job_id, _OTHER_USERNAME, is_admin=False)

    assert result["success"] is False, (
        "Bug #1070: cancel_job must reject non-admin user cancelling another's xray job"
    )
    assert not proc.terminate_called, "process must NOT be terminated when auth fails"
    assert proc.is_alive(), "process must still be alive when cancel is rejected"
