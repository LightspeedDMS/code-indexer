"""
Unit tests for Story #311: BackgroundJobManager + JobTracker Wiring.

Epic #261 Story 1B.

Tests are written FIRST (TDD) to define expected behavior before implementation:

AC1: BackgroundJobManager.__init__ accepts job_tracker parameter
AC2: submit_job registers with job_tracker.register_job()
AC3: _execute_job calls job_tracker.complete_job() on success
AC3: _execute_job calls job_tracker.fail_job() on failure
AC4: get_recent_jobs_with_filter API signature unchanged (backward compat)
AC5: BackgroundJobManager works normally when job_tracker=None (backward compat)
AC10: job_tracker.fail_job called when job is cancelled
"""

import sqlite3
import threading
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with the full background_jobs schema."""
    db = tmp_path / "test_bgm_tracker.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER NOT NULL DEFAULT 0,
        claude_actions TEXT,
        failure_reason TEXT,
        extended_error TEXT,
        language_resolution_status TEXT,
        progress_info TEXT,
        metadata TEXT
    )"""
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def tracker(db_path):
    """Create a real JobTracker connected to the temporary database."""
    return JobTracker(db_path)


@pytest.fixture
def manager_with_tracker(db_path, tracker):
    """BackgroundJobManager with SQLite + JobTracker wired in."""
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    cfg = BackgroundJobsConfig(max_concurrent_background_jobs=2)
    return BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=cfg,
        job_tracker=tracker,
    )


@pytest.fixture
def manager_without_tracker(db_path):
    """BackgroundJobManager with SQLite but NO JobTracker (backward compat fixture)."""
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    cfg = BackgroundJobsConfig(max_concurrent_background_jobs=2)
    return BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=cfg,
        # No job_tracker= argument
    )


# ---------------------------------------------------------------------------
# Helper: simple job functions
# ---------------------------------------------------------------------------


def simple_success_job() -> Dict[str, Any]:
    """Trivial job that returns a result dict immediately."""
    return {"status": "done"}


def simple_fail_job() -> Dict[str, Any]:
    """Trivial job that raises an exception."""
    raise RuntimeError("intentional failure")


def _wait_for_jobs(manager: BackgroundJobManager, timeout: float = 5.0) -> None:
    """Poll until all running threads in manager._running_jobs are done."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with manager._lock:
            if not manager._running_jobs:
                return
        time.sleep(0.05)
    raise TimeoutError("Jobs did not finish within timeout")


# ---------------------------------------------------------------------------
# AC1: __init__ accepts job_tracker parameter
# ---------------------------------------------------------------------------


class TestBackgroundJobManagerInit:
    """BackgroundJobManager.__init__ must accept and store job_tracker."""

    def test_init_accepts_job_tracker_parameter(self, db_path, tracker):
        """
        BackgroundJobManager accepts job_tracker keyword argument.

        Given a JobTracker instance
        When BackgroundJobManager is constructed with job_tracker=tracker
        Then no exception is raised and the tracker is stored
        """
        mgr = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            job_tracker=tracker,
        )
        assert mgr._job_tracker is tracker

    def test_init_job_tracker_defaults_to_none(self, db_path):
        """
        BackgroundJobManager works when job_tracker is not provided.

        Given no job_tracker argument
        When BackgroundJobManager is constructed
        Then _job_tracker is None
        """
        mgr = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
        )
        assert mgr._job_tracker is None


# ---------------------------------------------------------------------------
# AC2: submit_job calls job_tracker.register_job()
# ---------------------------------------------------------------------------


class TestSubmitJobRegistersWithTracker:
    """submit_job must call register_job on the tracker BEFORE spawning the thread."""

    def test_submit_job_calls_register_job(self, manager_with_tracker, tracker):
        """
        submit_job registers the job with the tracker.

        Given a BackgroundJobManager with a JobTracker wired in
        When submit_job is called
        Then a TrackedJob with the same job_id appears in the tracker
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                simple_success_job,
                submitter_username="admin",
                repo_alias="my-repo",
            )

        _wait_for_jobs(manager_with_tracker)

        # The job should be retrievable from the tracker (in memory or SQLite)
        job = tracker.get_job(job_id)
        assert job is not None
        assert job.job_id == job_id
        assert job.operation_type == "test_op"
        assert job.username == "admin"
        assert job.repo_alias == "my-repo"

    def test_submit_job_passes_correct_fields_to_tracker(
        self, manager_with_tracker, tracker
    ):
        """
        submit_job passes operation_type, username, and repo_alias to register_job.

        Given a submit_job call with specific parameters
        When the job completes
        Then the tracker has the correct metadata for that job
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "description_refresh",
                simple_success_job,
                submitter_username="user1",
                repo_alias="my-golden-repo",
            )

        _wait_for_jobs(manager_with_tracker)

        job = tracker.get_job(job_id)
        assert job is not None
        assert job.operation_type == "description_refresh"
        assert job.username == "user1"
        assert job.repo_alias == "my-golden-repo"

    def test_submit_job_without_tracker_does_not_crash(self, manager_without_tracker):
        """
        submit_job works normally when no tracker is configured.

        Given a BackgroundJobManager with job_tracker=None
        When submit_job is called
        Then the job runs successfully without any exception
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_without_tracker.submit_job(
                "test_op",
                simple_success_job,
                submitter_username="admin",
            )

        _wait_for_jobs(manager_without_tracker)
        assert isinstance(job_id, str) and len(job_id) > 0


# ---------------------------------------------------------------------------
# AC3: _execute_job calls complete_job on success, fail_job on failure
# ---------------------------------------------------------------------------


class TestExecuteJobTrackerCallbacks:
    """_execute_job must call tracker.complete_job() or tracker.fail_job()."""

    def test_successful_job_calls_complete_job_on_tracker(
        self, manager_with_tracker, tracker
    ):
        """
        _execute_job calls tracker.complete_job() when the job function returns.

        Given a job that completes successfully
        When the job finishes
        Then the tracker records the job as 'completed'
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                simple_success_job,
                submitter_username="admin",
            )

        _wait_for_jobs(manager_with_tracker)

        job = tracker.get_job(job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.completed_at is not None

    def test_failed_job_calls_fail_job_on_tracker(self, manager_with_tracker, tracker):
        """
        _execute_job calls tracker.fail_job() when the job function raises.

        Given a job function that raises RuntimeError
        When the job is submitted and runs
        Then the tracker records the job as 'failed' with the error message
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                simple_fail_job,
                submitter_username="admin",
            )

        _wait_for_jobs(manager_with_tracker)

        job = tracker.get_job(job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error is not None
        assert "intentional failure" in job.error

    def test_tracker_updates_to_running_before_completion(
        self, manager_with_tracker, tracker
    ):
        """
        The tracker transitions job to 'running' before calling complete_job.

        Given a job that takes a brief moment
        When the job finishes
        Then the tracker recorded started_at (transition through running state)
        """
        started_event = threading.Event()
        done_event = threading.Event()

        def gated_job():
            started_event.set()
            done_event.wait(timeout=5.0)
            return {"ok": True}

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                gated_job,
                submitter_username="admin",
            )

        # Wait for job to start running
        started_event.wait(timeout=5.0)

        # Let job finish
        done_event.set()
        _wait_for_jobs(manager_with_tracker)

        job = tracker.get_job(job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.started_at is not None

    def test_tracker_defensive_no_crash_on_tracker_failure(self, db_path):
        """
        If tracker.register_job raises, submit_job still works.

        Given a broken tracker that always raises
        When submit_job is called
        Then the job still runs (tracker failure is swallowed defensively)
        """
        from code_indexer.server.utils.config_manager import BackgroundJobsConfig

        broken_tracker = MagicMock()
        broken_tracker.register_job.side_effect = RuntimeError("tracker broken")
        broken_tracker.update_status.side_effect = RuntimeError("tracker broken")
        broken_tracker.complete_job.side_effect = RuntimeError("tracker broken")

        cfg = BackgroundJobsConfig(max_concurrent_background_jobs=2)
        mgr = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=cfg,
            job_tracker=broken_tracker,
        )

        completed = threading.Event()
        result_holder: Dict[str, Any] = {}

        def job_that_records():
            result_holder["ran"] = True
            completed.set()
            return {"ok": True}

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            # Should NOT raise even if tracker is broken
            mgr.submit_job(
                "test_op",
                job_that_records,
                submitter_username="admin",
            )

        completed.wait(timeout=5.0)
        _wait_for_jobs(mgr)

        # The job ran despite tracker failure
        assert result_holder.get("ran") is True


# ---------------------------------------------------------------------------
# AC4/AC5: Backward compatibility — API unchanged
# ---------------------------------------------------------------------------


class TestSubmitJobApiUnchanged:
    """submit_job signature must be identical to pre-story version."""

    def test_get_recent_jobs_with_filter_works_without_tracker(
        self, manager_without_tracker
    ):
        """
        get_recent_jobs_with_filter works when no tracker is configured.

        Given a BackgroundJobManager without a tracker
        When get_recent_jobs_with_filter is called
        Then it returns a list (possibly empty) without error
        """
        result = manager_without_tracker.get_recent_jobs_with_filter(
            time_filter="24h", limit=20
        )
        assert isinstance(result, list)

    def test_get_recent_jobs_with_filter_works_with_tracker(
        self, manager_with_tracker
    ):
        """
        get_recent_jobs_with_filter still works when tracker is configured.

        Given a BackgroundJobManager with a tracker
        When get_recent_jobs_with_filter is called
        Then it returns a list without error
        """
        result = manager_with_tracker.get_recent_jobs_with_filter(
            time_filter="24h", limit=20
        )
        assert isinstance(result, list)

    def test_submit_job_positional_and_keyword_args_work(self, manager_without_tracker):
        """
        submit_job accepts the same positional/keyword arguments as before.

        Given the existing submit_job signature
        When called with operation_type, func, and keyword args
        Then a job_id string is returned
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_without_tracker.submit_job(
                "test_op",
                simple_success_job,
                submitter_username="admin",
                is_admin=True,
                repo_alias="some-repo",
            )

        assert isinstance(job_id, str) and len(job_id) > 0
        _wait_for_jobs(manager_without_tracker)

    def test_submit_job_returns_string_job_id(self, manager_with_tracker):
        """
        submit_job always returns a non-empty string job ID.

        Given a submit_job call
        When the call succeeds
        Then a non-empty string job_id is returned
        """
        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                simple_success_job,
                submitter_username="admin",
            )

        assert isinstance(job_id, str) and len(job_id) > 0
        _wait_for_jobs(manager_with_tracker)


# ---------------------------------------------------------------------------
# AC10: Cancelled job calls tracker.fail_job
# ---------------------------------------------------------------------------


class TestCancelledJobTrackerCallback:
    """When a job is cancelled, the tracker must record it as failed/cancelled."""

    def test_cancelled_job_calls_fail_job_on_tracker(
        self, manager_with_tracker, tracker
    ):
        """
        _execute_job calls tracker.fail_job() when job is cancelled.

        Given a running job that gets cancelled while waiting for the semaphore slot
        When the job's cancelled flag is set before the thread picks it up
        Then the tracker records the job as failed or cancelled
        """
        # Use a gated job so we can cancel it before it runs
        ready_event = threading.Event()
        gate_event = threading.Event()

        def gated_job():
            ready_event.set()
            gate_event.wait(timeout=5.0)
            return {"ok": True}

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False

            job_id = manager_with_tracker.submit_job(
                "test_op",
                gated_job,
                submitter_username="admin",
            )

        # Wait for job to start running
        ready_event.wait(timeout=5.0)

        # Cancel the job while it is running
        manager_with_tracker.cancel_job(job_id, "admin")

        # Release the gate so the job thread can proceed and observe cancellation
        gate_event.set()
        _wait_for_jobs(manager_with_tracker)

        # After cancellation, the tracker should record the job as failed or cancelled
        job = tracker.get_job(job_id)
        assert job is not None
        assert job.status in ("failed", "cancelled")
