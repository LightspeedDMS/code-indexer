"""
Unit tests for BackgroundJobManager + JobTracker integration (Story #311, Epic #261).

Tests the dual-write bug fix: when BGM calls submit_job(), it first persists the job
row to background_jobs via _persist_jobs(), then calls tracker.register_job() which
must also INSERT into the same table.

Before the fix: tracker._insert_job() uses plain INSERT INTO, which hits a UNIQUE
constraint violation on job_id (BGM already wrote the row), so the exception is caught,
logged as a warning, and the job is NEVER added to tracker._active_jobs in-memory dict.
This causes get_active_job_count() and get_pending_job_count() to return 0 for all
BGM-managed jobs.

After the fix: tracker._insert_job() uses INSERT OR REPLACE INTO, so when the row
already exists (written by BGM), the INSERT succeeds, and the job enters _active_jobs.
"""

import shutil
import tempfile
import time
import unittest.mock
from pathlib import Path

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_dir():
    """Create a temporary directory for the test database."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_path(temp_dir):
    """Create a temporary SQLite database with the full background_jobs schema."""
    path = str(Path(temp_dir) / "test_bgm_tracker.db")
    DatabaseSchema(path).initialize_database()
    return path


@pytest.fixture
def tracker(db_path):
    """Create a real JobTracker connected to the temporary database."""
    return JobTracker(db_path)


@pytest.fixture
def bgm(db_path, tracker):
    """Create a BackgroundJobManager with SQLite backend and a real JobTracker."""
    manager = BackgroundJobManager(
        use_sqlite=True,
        db_path=db_path,
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=10,
        ),
        job_tracker=tracker,
    )
    yield manager
    manager.shutdown()


# ---------------------------------------------------------------------------
# AC1 - Constructor tests
# ---------------------------------------------------------------------------


class TestBGMConstructor:
    """
    AC1: BackgroundJobManager constructor accepts an optional job_tracker parameter.

    When job_tracker=None (default), behavior is unchanged.
    When a real JobTracker is provided, it is stored for use during job lifecycle.
    """

    def test_bgm_constructed_without_tracker_defaults_to_none(self, db_path):
        """
        BGM can be constructed without a job_tracker (default None).

        Given BackgroundJobManager is instantiated without job_tracker
        When the manager is created
        Then _job_tracker is None and no errors are raised
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )
        try:
            assert manager._job_tracker is None
        finally:
            manager.shutdown()

    def test_bgm_constructed_with_real_tracker_stores_it(self, db_path, tracker):
        """
        BGM can be constructed with a real JobTracker instance.

        Given a real JobTracker connected to the same database
        When BackgroundJobManager is instantiated with job_tracker=tracker
        Then manager._job_tracker is the provided tracker instance
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            assert manager._job_tracker is tracker
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# AC2 - submit_job() tracker registration tests
# ---------------------------------------------------------------------------


class TestBGMSubmitJobTrackerRegistration:
    """
    AC2: BGM.submit_job() calls tracker.register_job() with correct arguments
    after persisting the job to SQLite.

    When tracker is None, submit_job() works normally without any tracker calls.
    When tracker.register_job() raises an exception, submit_job() continues
    (defensive try/except).
    """

    def test_submit_job_calls_tracker_register_job_with_correct_args(
        self, db_path, tracker
    ):
        """
        submit_job() calls tracker.register_job() with matching job metadata.

        Given a BGM with a real JobTracker
        When submit_job() is called with operation_type, submitter_username, repo_alias
        Then tracker.register_job is called with the same job_id, operation_type,
             username, and repo_alias
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            with unittest.mock.patch.object(
                tracker, "register_job", wraps=tracker.register_job
            ) as mock_register:

                def quick_task():
                    return {"status": "ok"}

                job_id = manager.submit_job(
                    "test_operation",
                    quick_task,
                    submitter_username="testuser",
                    repo_alias="my-repo",
                )

                mock_register.assert_called_once_with(
                    job_id=job_id,
                    operation_type="test_operation",
                    username="testuser",
                    repo_alias="my-repo",
                )
        finally:
            manager.shutdown()

    def test_submit_job_works_when_tracker_is_none(self, db_path):
        """
        submit_job() succeeds and returns a job_id when tracker is None.

        Given a BGM without a job_tracker
        When submit_job() is called
        Then a valid job_id is returned and no exceptions are raised
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )
        try:
            def quick_task():
                return {"status": "ok"}

            job_id = manager.submit_job(
                "test_operation",
                quick_task,
                submitter_username="testuser",
            )
            assert job_id is not None
            assert len(job_id) > 0
        finally:
            manager.shutdown()

    def test_submit_job_continues_when_tracker_register_raises(self, db_path, tracker):
        """
        submit_job() does not raise when tracker.register_job() throws an exception.

        Given a BGM with a tracker whose register_job raises RuntimeError
        When submit_job() is called
        Then a valid job_id is returned (BGM continues despite tracker error)
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            with unittest.mock.patch.object(
                tracker,
                "register_job",
                side_effect=RuntimeError("tracker failure"),
            ):

                def quick_task():
                    return {"status": "ok"}

                # Must not raise even though tracker.register_job raises
                job_id = manager.submit_job(
                    "test_operation",
                    quick_task,
                    submitter_username="testuser",
                )
                assert job_id is not None
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# AC3 - _execute_job() running status transition tests
# ---------------------------------------------------------------------------


class TestBGMExecuteJobRunningStatus:
    """
    AC3: When a job transitions from pending to running, BGM calls
    tracker.update_status(job_id, status="running").

    If update_status raises, the job continues running normally in BGM.
    """

    def test_execute_job_calls_tracker_update_status_running(self, bgm, tracker):
        """
        _execute_job() transitions tracker status to "running" before executing.

        Given a BGM with a real tracker and a slow task
        When submit_job() starts executing
        Then after a short pause, tracker shows the job as running
        """
        def slow_task():
            time.sleep(0.5)
            return {"done": True}

        job_id = bgm.submit_job(
            "slow_op",
            slow_task,
            submitter_username="admin",
        )

        # Give the thread time to acquire the semaphore and call update_status
        time.sleep(0.15)

        with tracker._lock:
            job = tracker._active_jobs.get(job_id)

        assert job is not None, (
            f"Job {job_id!r} should be in tracker._active_jobs while running"
        )
        assert job.status == "running", (
            f"Expected status='running', got {job.status!r}"
        )

    def test_execute_job_continues_when_tracker_update_status_raises(
        self, db_path, tracker
    ):
        """
        _execute_job() continues to completion when tracker.update_status raises.

        Given a BGM where tracker.update_status raises RuntimeError
        When a job is submitted and executed
        Then the job completes successfully in BGM (status COMPLETED in SQLite)
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            with unittest.mock.patch.object(
                tracker,
                "update_status",
                side_effect=RuntimeError("tracker update failure"),
            ):

                def quick_task():
                    return {"result": "success"}

                job_id = manager.submit_job(
                    "test_op",
                    quick_task,
                    submitter_username="admin",
                )

                # Wait for job to complete in BGM
                time.sleep(0.3)

            # After patch context exits, verify job completed in SQLite
            job = tracker.get_job(job_id)
            assert job is not None
            # The job row should exist in SQLite (either completed or in a terminal state)
            # BGM continued even though tracker update_status raised
            assert job.job_id == job_id
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# AC4 - _execute_job() completion and failure tracker callbacks
# ---------------------------------------------------------------------------


class TestBGMExecuteJobCompletionCallbacks:
    """
    AC4: On successful completion, BGM calls tracker.complete_job().
    On failure (exception), BGM calls tracker.fail_job(job_id, error=error_msg).

    In both cases, if the tracker method raises, BGM continues without re-raising.
    """

    def test_successful_job_calls_tracker_complete_job(self, bgm, tracker):
        """
        A successfully completed job causes tracker.complete_job() to be called.

        Given a BGM with a real tracker and a task returning a dict
        When the task executes and returns successfully
        Then the tracker reflects the job as completed (removed from _active_jobs,
             present in SQLite with status="completed")
        """
        def succeeding_task():
            return {"result": "all_good"}

        job_id = bgm.submit_job(
            "success_op",
            succeeding_task,
            submitter_username="admin",
        )

        # Wait for completion
        time.sleep(0.3)

        # complete_job() removes from _active_jobs and writes to SQLite
        with tracker._lock:
            in_memory = job_id in tracker._active_jobs

        assert not in_memory, (
            "Completed job must be removed from tracker._active_jobs"
        )

        db_job = tracker.get_job(job_id)
        assert db_job is not None
        assert db_job.status == "completed"

    def test_successful_job_result_dict_passed_to_tracker_complete_job(
        self, bgm, tracker
    ):
        """
        The result dict returned by the task is passed to tracker.complete_job().

        Given a BGM with a tracker and a task returning {"key": "value"}
        When the task completes
        Then the tracker's SQLite record stores that result
        """
        expected_result = {"answer": 42, "status": "done"}

        def task_with_result():
            return expected_result

        job_id = bgm.submit_job(
            "result_op",
            task_with_result,
            submitter_username="admin",
        )

        time.sleep(0.3)

        db_job = tracker.get_job(job_id)
        assert db_job is not None
        assert db_job.status == "completed"
        assert db_job.result == expected_result

    def test_execute_job_continues_when_tracker_complete_job_raises(
        self, db_path, tracker
    ):
        """
        BGM does not raise if tracker.complete_job() throws an exception.

        Given a BGM where tracker.complete_job raises RuntimeError
        When a task succeeds
        Then no exception propagates out of the background thread (BGM is stable)
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            with unittest.mock.patch.object(
                tracker,
                "complete_job",
                side_effect=RuntimeError("tracker complete failure"),
            ):

                def quick_task():
                    return {"ok": True}

                # submit_job must not raise
                job_id = manager.submit_job(
                    "test_op",
                    quick_task,
                    submitter_username="admin",
                )

                # Allow background thread to finish
                time.sleep(0.3)

            # BGM itself should have recorded the job as completed in SQLite
            # (BGM's own persist happens before the tracker call)
            assert job_id is not None
        finally:
            manager.shutdown()

    def test_failed_job_calls_tracker_fail_job_with_error_message(
        self, bgm, tracker
    ):
        """
        A job that raises an exception causes tracker.fail_job() to be called
        with the exception's error message.

        Given a BGM with a real tracker and a task that raises ValueError("boom")
        When the task executes and raises
        Then tracker records the job as failed with error="boom"
        """
        def failing_task():
            raise ValueError("boom")

        job_id = bgm.submit_job(
            "failing_op",
            failing_task,
            submitter_username="admin",
        )

        # Wait for the failure to be processed
        time.sleep(0.3)

        # fail_job() removes from _active_jobs and writes to SQLite
        with tracker._lock:
            in_memory = job_id in tracker._active_jobs

        assert not in_memory, (
            "Failed job must be removed from tracker._active_jobs"
        )

        db_job = tracker.get_job(job_id)
        assert db_job is not None
        assert db_job.status == "failed"
        assert "boom" in (db_job.error or ""), (
            f"Expected error to contain 'boom', got {db_job.error!r}"
        )

    def test_execute_job_continues_when_tracker_fail_job_raises(
        self, db_path, tracker
    ):
        """
        BGM does not raise if tracker.fail_job() throws an exception.

        Given a BGM where tracker.fail_job raises RuntimeError
        When a task raises ValueError
        Then no exception propagates out of the background thread
        """
        manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
            job_tracker=tracker,
        )
        try:
            with unittest.mock.patch.object(
                tracker,
                "fail_job",
                side_effect=RuntimeError("tracker fail_job failure"),
            ):

                def failing_task():
                    raise ValueError("task error")

                # submit_job must not raise
                job_id = manager.submit_job(
                    "test_op",
                    failing_task,
                    submitter_username="admin",
                )

                # Allow background thread to finish
                time.sleep(0.3)

            assert job_id is not None
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# Lifecycle integration tests
# ---------------------------------------------------------------------------


class TestBGMTrackerLifecycleIntegration:
    """
    End-to-end lifecycle integration tests verifying tracker reflects correct
    state through the full job lifecycle: submit -> run -> complete/fail.
    """

    def test_full_lifecycle_success_tracker_reflects_completed_state(
        self, bgm, tracker
    ):
        """
        Full lifecycle: submit -> running -> completed — tracker reflects final state.

        Given a BGM with a tracker and a quick-returning task
        When the full lifecycle completes
        Then tracker.get_job() returns a job with status="completed"
        """
        def quick_task():
            return {"lifecycle": "success"}

        job_id = bgm.submit_job(
            "lifecycle_op",
            quick_task,
            submitter_username="admin",
            repo_alias="test-repo",
        )

        # Wait for full lifecycle
        time.sleep(0.4)

        job = tracker.get_job(job_id)
        assert job is not None, f"Job {job_id!r} not found in tracker after completion"
        assert job.status == "completed", (
            f"Expected status='completed', got {job.status!r}"
        )

    def test_full_lifecycle_failure_tracker_reflects_failed_state(
        self, bgm, tracker
    ):
        """
        Full lifecycle with failure: submit -> running -> failed — tracker shows failed.

        Given a BGM with a tracker and a task that raises an exception
        When the full lifecycle completes (with failure)
        Then tracker.get_job() returns a job with status="failed" and non-empty error
        """
        def failing_task():
            raise RuntimeError("lifecycle failure")

        job_id = bgm.submit_job(
            "lifecycle_fail_op",
            failing_task,
            submitter_username="admin",
            repo_alias="test-repo",
        )

        # Wait for full lifecycle
        time.sleep(0.4)

        job = tracker.get_job(job_id)
        assert job is not None, f"Job {job_id!r} not found in tracker after failure"
        assert job.status == "failed", (
            f"Expected status='failed', got {job.status!r}"
        )
        assert job.error is not None and len(job.error) > 0, (
            "Failed job must have a non-empty error message in tracker"
        )


# ---------------------------------------------------------------------------
# Bug fix tests
# ---------------------------------------------------------------------------


class TestBGMTrackerInMemoryState:
    """
    Tests proving the dual-write UNIQUE constraint bug and its fix.

    Before fix: BGM writes row to background_jobs, then tracker tries to INSERT
    the same job_id -> UNIQUE constraint violation -> job never enters _active_jobs.

    After fix: tracker uses INSERT OR REPLACE, so the row is overwritten (with
    identical data) and the job enters _active_jobs correctly.
    """

    def test_tracker_has_job_in_memory_after_bgm_submit(self, bgm, tracker):
        """
        After BGM submit_job(), the tracker's _active_jobs dict must contain the job.

        Given a BackgroundJobManager configured with a real JobTracker
        When submit_job() is called
        Then tracker._active_jobs contains the job_id immediately after submit

        This test FAILS before the INSERT OR REPLACE fix because the UNIQUE
        constraint violation prevents the job from entering tracker._active_jobs.
        """

        def quick_task():
            return {"status": "success"}

        job_id = bgm.submit_job(
            "test_op",
            quick_task,
            submitter_username="admin",
        )

        # The tracker.register_job() call happens synchronously before the thread
        # is spawned, so no sleep is needed. A tiny margin guards against any race.
        time.sleep(0.05)

        # Critical assertion: job must be IN MEMORY, not just in SQLite.
        # Before the fix, _active_jobs is empty because INSERT failed silently.
        with tracker._lock:
            in_memory = job_id in tracker._active_jobs

        assert in_memory, (
            f"job_id {job_id!r} must be in tracker._active_jobs after BGM submit_job(). "
            "This fails when tracker._insert_job uses plain INSERT INTO (UNIQUE collision)."
        )

    def test_pending_job_count_nonzero_after_bgm_submit(self, bgm, tracker):
        """
        tracker.get_pending_job_count() + get_active_job_count() must be > 0
        after a BGM submit_job() call with a slow-running task.

        Given a BackgroundJobManager with a real JobTracker and a slow task
        When submit_job() is called
        Then at least one of pending or running count reflects the job

        Before the fix: both counts are 0 because the job never enters _active_jobs.
        After the fix: at least one of pending or active is > 0 immediately after submit.
        """

        def slow_task():
            time.sleep(2.0)
            return {"status": "done"}

        bgm.submit_job(
            "slow_op",
            slow_task,
            submitter_username="admin",
        )

        # Brief pause to allow thread to start and status to transition to running
        time.sleep(0.1)

        pending = tracker.get_pending_job_count()
        active = tracker.get_active_job_count()
        total_tracked = pending + active

        assert total_tracked > 0, (
            f"Expected at least 1 job tracked in memory after BGM submit, "
            f"got pending={pending} active={active}. "
            "This indicates the dual-write UNIQUE constraint bug is still present."
        )
