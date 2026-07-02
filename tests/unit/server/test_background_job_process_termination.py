"""
Unit tests for BackgroundJobManager process tracking and termination.

Story #996: MCP cancel_job with XRay process termination.

Tests cover:
- register_child_process stores process references
- unregister_child_processes cleans up references
- _terminate_child_processes sends SIGTERM then SIGKILL
- cancel_job terminates processes for running jobs
- Race condition: cancel before process registration
"""

from __future__ import annotations

import multiprocessing
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bjm() -> BackgroundJobManager:
    """Create a BackgroundJobManager without maintenance mode or persistence."""
    with patch(
        "code_indexer.server.services.maintenance_service.get_maintenance_state"
    ) as mock_maint:
        mock_maint.return_value.is_maintenance_mode.return_value = False
        bjm = BackgroundJobManager()
    return bjm


def _insert_job(
    bjm: BackgroundJobManager, job_id: str, status: JobStatus, username: str = "alice"
) -> BackgroundJob:
    """Directly insert a job into the manager bypassing submit_job."""
    job = BackgroundJob(
        job_id=job_id,
        operation_type="xray_search",
        status=status,
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc) if status == JobStatus.RUNNING else None,
        completed_at=None,
        result=None,
        error=None,
        progress=0,
        username=username,
    )
    with bjm._lock:
        bjm.jobs[job_id] = job
    return job


# ---------------------------------------------------------------------------
# Tests: register_child_process
# ---------------------------------------------------------------------------


class TestRegisterChildProcess:
    """BackgroundJobManager.register_child_process stores process references."""

    def test_register_child_process_stores_process(self):
        """register_child_process stores one process for a job_id."""
        bjm = _make_bjm()
        mock_proc = MagicMock(spec=multiprocessing.Process)
        mock_proc.is_alive.return_value = True

        bjm.register_child_process("job-1", mock_proc)

        with bjm._child_processes_lock:
            procs = bjm._child_processes.get("job-1", [])
        assert len(procs) == 1
        assert procs[0] is mock_proc

    def test_register_child_process_multiple(self):
        """register_child_process appends multiple processes for the same job_id."""
        bjm = _make_bjm()
        proc1 = MagicMock(spec=multiprocessing.Process)
        proc2 = MagicMock(spec=multiprocessing.Process)
        proc1.is_alive.return_value = True
        proc2.is_alive.return_value = True

        bjm.register_child_process("job-multi", proc1)
        bjm.register_child_process("job-multi", proc2)

        with bjm._child_processes_lock:
            procs = bjm._child_processes.get("job-multi", [])
        assert len(procs) == 2
        assert proc1 in procs
        assert proc2 in procs


# ---------------------------------------------------------------------------
# Tests: unregister_child_processes
# ---------------------------------------------------------------------------


class TestUnregisterChildProcesses:
    """BackgroundJobManager.unregister_child_processes cleans up correctly."""

    def test_unregister_child_processes_cleans_up(self):
        """unregister_child_processes removes all processes for a job_id."""
        bjm = _make_bjm()
        mock_proc = MagicMock(spec=multiprocessing.Process)
        bjm.register_child_process("job-cleanup", mock_proc)

        bjm.unregister_child_processes("job-cleanup")

        with bjm._child_processes_lock:
            procs = bjm._child_processes.get("job-cleanup")
        assert procs is None

    def test_unregister_idempotent(self):
        """unregister_child_processes does not raise when job_id has no processes."""
        bjm = _make_bjm()

        # Should not raise even with unknown job_id
        bjm.unregister_child_processes("nonexistent-job")

        with bjm._child_processes_lock:
            assert "nonexistent-job" not in bjm._child_processes


# ---------------------------------------------------------------------------
# Tests: _terminate_child_processes
# ---------------------------------------------------------------------------


class TestTerminateChildProcesses:
    """BackgroundJobManager._terminate_child_processes sends signals correctly."""

    def test_terminate_sends_sigterm(self):
        """_terminate_child_processes calls terminate() on alive processes."""
        bjm = _make_bjm()
        mock_proc = MagicMock(spec=multiprocessing.Process)
        # Returns True for the is_alive() check before terminate(), False for
        # the is_alive() check after join() so SIGKILL is NOT sent.
        mock_proc.is_alive.side_effect = [True, False]
        bjm.register_child_process("job-sigterm", mock_proc)

        bjm._terminate_child_processes("job-sigterm")

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

    def test_terminate_escalates_to_sigkill(self):
        """_terminate_child_processes sends SIGKILL if process survives SIGTERM grace period."""
        bjm = _make_bjm()
        mock_proc = MagicMock(spec=multiprocessing.Process)
        # Process stays alive after terminate() + join(timeout=2.0)
        mock_proc.is_alive.return_value = True

        bjm.register_child_process("job-sigkill", mock_proc)
        bjm._terminate_child_processes("job-sigkill")

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_terminate_skips_dead_process(self):
        """_terminate_child_processes does not call terminate() on dead process."""
        bjm = _make_bjm()
        mock_proc = MagicMock(spec=multiprocessing.Process)
        mock_proc.is_alive.return_value = False

        # Patch so is_alive returns False even before terminate
        bjm.register_child_process("job-dead", mock_proc)
        # Override to make it dead from the start
        mock_proc.is_alive.return_value = False

        bjm._terminate_child_processes("job-dead")

        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: cancel_job integration with process termination
# ---------------------------------------------------------------------------


class TestCancelJobTerminatesProcesses:
    """cancel_job triggers process termination for running jobs."""

    def test_cancel_job_terminates_running_processes(self):
        """cancel_job calls _terminate_child_processes when job is RUNNING."""
        bjm = _make_bjm()
        _insert_job(bjm, "job-running", JobStatus.RUNNING, username="alice")
        mock_proc = MagicMock(spec=multiprocessing.Process)
        # Returns True for the is_alive() check before terminate(), False for
        # the is_alive() check after join() so SIGKILL is NOT sent.
        mock_proc.is_alive.side_effect = [True, False]
        bjm.register_child_process("job-running", mock_proc)

        with patch.object(bjm, "_persist_jobs"):
            result = bjm.cancel_job("job-running", "alice", is_admin=False)

        assert result["success"] is True
        mock_proc.terminate.assert_called_once()

    def test_cancel_job_pending_no_terminate(self):
        """cancel_job does NOT call _terminate_child_processes for PENDING jobs."""
        bjm = _make_bjm()
        _insert_job(bjm, "job-pending", JobStatus.PENDING, username="alice")

        with patch.object(bjm, "_persist_jobs"):
            with patch.object(bjm, "_terminate_child_processes") as mock_term:
                result = bjm.cancel_job("job-pending", "alice", is_admin=False)

        assert result["success"] is True
        mock_term.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: race condition (AC7)
# ---------------------------------------------------------------------------


class TestRaceConditionCancelBeforeRegister:
    """AC7: cancel arriving before process registration terminates the process."""

    def test_race_condition_cancel_before_register(self):
        """When job is already cancelled, register_child_process terminates the process."""
        bjm = _make_bjm()
        _insert_job(bjm, "job-race", JobStatus.RUNNING, username="alice")

        # Mark job as cancelled (simulate cancel arriving first)
        with bjm._lock:
            bjm.jobs["job-race"].cancelled = True

        mock_proc = MagicMock(spec=multiprocessing.Process)
        mock_proc.is_alive.return_value = False

        with patch.object(bjm, "_terminate_child_processes") as mock_term:
            bjm.register_child_process("job-race", mock_proc)

        # The method should detect the cancelled flag and call terminate
        mock_term.assert_called_once_with("job-race")


# ---------------------------------------------------------------------------
# Tests: exception-after-cancel yields CANCELLED not FAILED
# ---------------------------------------------------------------------------


class TestExceptionAfterCancelYieldsCancelled:
    """When process termination causes an exception, status must be CANCELLED."""

    def test_cancelled_job_exception_yields_cancelled(self):
        """If job.cancelled is True when exception fires, status = CANCELLED."""
        import threading
        import time

        bjm = _make_bjm()
        barrier = threading.Event()

        def slow_then_fail():
            barrier.wait(timeout=5)
            raise RuntimeError("terminated")

        with patch.object(bjm, "_persist_jobs"):
            job_id = bjm.submit_job(
                "xray_search", slow_then_fail, submitter_username="alice"
            )

        time.sleep(0.3)

        with patch.object(bjm, "_persist_jobs"):
            bjm.cancel_job(job_id, "alice", is_admin=False)

        barrier.set()
        time.sleep(1)

        with bjm._lock:
            job = bjm.jobs.get(job_id)

        if job is not None:
            assert job.status == JobStatus.CANCELLED
            assert job.error in ("cancelled", "Job cancelled during execution")
