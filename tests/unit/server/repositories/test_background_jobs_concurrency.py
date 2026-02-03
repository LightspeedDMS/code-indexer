"""
Unit tests for BackgroundJobManager concurrency limiting (Story #26).

Tests the concurrent job limit feature that prevents resource exhaustion
when many jobs are submitted simultaneously.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import tempfile
import time
import threading
from pathlib import Path


from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.utils.config_manager import (
    BackgroundJobsConfig,
)


class TestBackgroundJobManagerConcurrencyLimit:
    """Test BackgroundJobManager concurrent job limiting (Story #26)."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        import shutil
        import os

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    # ==========================================================================
    # AC1: Add max_concurrent_background_jobs setting to config
    # ==========================================================================

    def test_manager_accepts_background_jobs_config(self):
        """AC1: BackgroundJobManager should accept BackgroundJobsConfig."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=3)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )
        assert self.manager is not None

    def test_manager_uses_config_max_concurrent_jobs(self):
        """AC1: Manager should use max_concurrent_background_jobs from config."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=3)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )
        assert self.manager.max_concurrent_jobs == 3

    def test_manager_default_max_concurrent_jobs_is_five(self):
        """AC1: Default max concurrent jobs should be 5."""
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
        )
        assert self.manager.max_concurrent_jobs == 5

    # ==========================================================================
    # AC2: Implement job queue with concurrency limiting
    # ==========================================================================

    def test_concurrency_limit_enforced(self):
        """AC2: Only max_concurrent jobs should run simultaneously."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=2)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        # Track how many jobs run concurrently
        concurrent_count = []
        current_running = [0]  # Use list to allow modification in closure
        lock = threading.Lock()

        def slow_job():
            with lock:
                current_running[0] += 1
                concurrent_count.append(current_running[0])
            time.sleep(0.3)  # Long enough to overlap with other jobs
            with lock:
                current_running[0] -= 1
            return {"status": "success"}

        # Submit 5 jobs with limit of 2
        job_ids = []
        for i in range(5):
            job_id = self.manager.submit_job(
                f"test_job_{i}",
                slow_job,
                submitter_username="test_user",
            )
            job_ids.append(job_id)

        # Wait for all jobs to complete
        time.sleep(2.0)

        # Maximum concurrent jobs should never exceed the limit (2)
        assert (
            max(concurrent_count) <= 2
        ), f"Concurrent count exceeded limit: {concurrent_count}"

    # ==========================================================================
    # AC3: Jobs exceeding limit stay in PENDING until slot available
    # ==========================================================================

    def test_jobs_exceeding_limit_stay_pending(self):
        """AC3: Jobs beyond the limit should stay in PENDING state."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=2)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        started_event = threading.Event()
        release_event = threading.Event()

        def blocking_job():
            started_event.set()
            release_event.wait(timeout=5.0)  # Block until released
            return {"status": "success"}

        def fast_job():
            return {"status": "success"}

        # Submit 3 blocking jobs with limit of 2
        job_ids = []
        for i in range(3):
            job_id = self.manager.submit_job(
                f"blocking_job_{i}",
                blocking_job,
                submitter_username="test_user",
            )
            job_ids.append(job_id)

        # Wait a moment for first 2 to start
        time.sleep(0.2)

        # Check job states
        running_count = 0
        pending_count = 0
        with self.manager._lock:
            for job_id in job_ids:
                job = self.manager.jobs.get(job_id)
                if job:
                    if job.status == JobStatus.RUNNING:
                        running_count += 1
                    elif job.status == JobStatus.PENDING:
                        pending_count += 1

        # Should have 2 running and 1 pending
        assert running_count == 2, f"Expected 2 running jobs, got {running_count}"
        assert pending_count == 1, f"Expected 1 pending job, got {pending_count}"

        # Release the blocking jobs
        release_event.set()

        # Wait for completion
        time.sleep(0.5)

    def test_pending_jobs_start_when_slot_becomes_available(self):
        """AC3: Pending jobs should start when a slot becomes available."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        execution_order = []
        lock = threading.Lock()

        def tracked_job(job_name):
            with lock:
                execution_order.append(f"start_{job_name}")
            time.sleep(0.1)
            with lock:
                execution_order.append(f"end_{job_name}")
            return {"status": "success"}

        # Submit 3 jobs with limit of 1
        job1_id = self.manager.submit_job(
            "job1", lambda: tracked_job("job1"), submitter_username="test_user"
        )
        job2_id = self.manager.submit_job(
            "job2", lambda: tracked_job("job2"), submitter_username="test_user"
        )
        job3_id = self.manager.submit_job(
            "job3", lambda: tracked_job("job3"), submitter_username="test_user"
        )

        # Wait for all to complete
        time.sleep(1.5)

        # Jobs should have executed sequentially (one at a time)
        # Execution order should show start_job1, end_job1, start_job2, ...
        # (though exact order of job2 and job3 may vary)
        assert len(execution_order) == 6, f"Expected 6 events, got {execution_order}"

        # Verify all jobs completed
        for job_id in [job1_id, job2_id, job3_id]:
            status = self.manager.get_job_status(job_id, username="test_user")
            assert status is not None
            assert status["status"] == "completed"

    # ==========================================================================
    # AC5: Add monitoring - current running count, queue depth
    # ==========================================================================

    def test_get_running_job_count(self):
        """AC5: Should be able to get current running job count."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=2)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        started_event = threading.Event()
        release_event = threading.Event()

        def blocking_job():
            started_event.set()
            release_event.wait(timeout=5.0)
            return {"status": "success"}

        # Submit 2 blocking jobs
        self.manager.submit_job("job1", blocking_job, submitter_username="test_user")
        self.manager.submit_job("job2", blocking_job, submitter_username="test_user")

        # Wait for jobs to start
        time.sleep(0.2)

        # Check running count
        running_count = self.manager.get_running_job_count()
        assert running_count == 2

        # Release and wait for completion
        release_event.set()
        time.sleep(0.5)

        # After completion, running count should be 0
        running_count = self.manager.get_running_job_count()
        assert running_count == 0

    def test_get_queued_job_count(self):
        """AC5: Should be able to get queue depth (pending jobs waiting)."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        started_event = threading.Event()
        release_event = threading.Event()

        def blocking_job():
            started_event.set()
            release_event.wait(timeout=5.0)
            return {"status": "success"}

        # Submit 4 jobs with limit of 1
        self.manager.submit_job("job1", blocking_job, submitter_username="test_user")
        time.sleep(0.1)  # Let first job start
        self.manager.submit_job("job2", blocking_job, submitter_username="test_user")
        self.manager.submit_job("job3", blocking_job, submitter_username="test_user")
        self.manager.submit_job("job4", blocking_job, submitter_username="test_user")

        # Wait for first job to start running
        time.sleep(0.2)

        # Check queue depth (should be 3 waiting)
        queued_count = self.manager.get_queued_job_count()
        assert queued_count == 3, f"Expected 3 queued jobs, got {queued_count}"

        # Release and wait
        release_event.set()
        time.sleep(2.0)

        # After all complete, queue should be empty
        queued_count = self.manager.get_queued_job_count()
        assert queued_count == 0

    def test_get_job_queue_metrics(self):
        """AC5: Should have combined metrics method for running/queued counts."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=2)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        release_event = threading.Event()

        def blocking_job():
            release_event.wait(timeout=5.0)
            return {"status": "success"}

        # Submit 5 jobs with limit of 2
        for i in range(5):
            self.manager.submit_job(
                f"job{i}", blocking_job, submitter_username="test_user"
            )

        # Wait for jobs to settle
        time.sleep(0.3)

        # Get combined metrics
        metrics = self.manager.get_job_queue_metrics()

        assert "running_count" in metrics
        assert "queued_count" in metrics
        assert "max_concurrent" in metrics

        assert metrics["running_count"] == 2
        assert metrics["queued_count"] == 3
        assert metrics["max_concurrent"] == 2

        # Release all
        release_event.set()
        time.sleep(1.5)

    # ==========================================================================
    # AC6: Unit tests verify concurrency limit is enforced
    # ==========================================================================

    def test_concurrency_limit_with_rapid_submissions(self):
        """AC6: Limit should be enforced even with rapid job submissions."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=3)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        max_observed_concurrent = [0]
        current_count = [0]
        lock = threading.Lock()

        def tracked_job():
            with lock:
                current_count[0] += 1
                if current_count[0] > max_observed_concurrent[0]:
                    max_observed_concurrent[0] = current_count[0]
            time.sleep(0.2)
            with lock:
                current_count[0] -= 1
            return {"status": "success"}

        # Submit 20 jobs rapidly
        job_ids = []
        for i in range(20):
            job_id = self.manager.submit_job(
                f"rapid_job_{i}",
                tracked_job,
                submitter_username="test_user",
            )
            job_ids.append(job_id)

        # Wait for all to complete
        time.sleep(5.0)

        # Verify limit was never exceeded
        assert (
            max_observed_concurrent[0] <= 3
        ), f"Limit exceeded: max concurrent was {max_observed_concurrent[0]}"

        # Verify all jobs completed
        for job_id in job_ids:
            status = self.manager.get_job_status(job_id, username="test_user")
            assert status["status"] == "completed"


class TestBackgroundJobManagerConcurrencyIntegration:
    """Integration tests for concurrent job limiting with existing features."""

    def setup_method(self):
        """Setup test environment."""
        self.temp_dir = tempfile.mkdtemp()
        self.job_storage_path = Path(self.temp_dir) / "jobs.json"

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        import shutil
        import os

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_cancellation_frees_slot(self):
        """Cancelling a running job should free a slot for pending jobs."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        started_events = []
        for _ in range(2):
            started_events.append(threading.Event())

        execution_log = []

        def blocking_job_1():
            started_events[0].set()
            execution_log.append("job1_started")
            time.sleep(5.0)  # Will be cancelled before completion
            return {"status": "success"}

        def fast_job_2():
            started_events[1].set()
            execution_log.append("job2_started")
            return {"status": "success"}

        # Submit first (blocking) job
        job1_id = self.manager.submit_job(
            "blocking_job", blocking_job_1, submitter_username="test_user"
        )

        # Wait for first job to start
        started_events[0].wait(timeout=1.0)

        # Submit second job (should be pending)
        job2_id = self.manager.submit_job(
            "waiting_job", fast_job_2, submitter_username="test_user"
        )

        # Verify job2 is pending
        time.sleep(0.2)
        job2_status = self.manager.get_job_status(job2_id, username="test_user")
        assert job2_status["status"] == "pending"

        # Cancel job1
        result = self.manager.cancel_job(job1_id, username="test_user")
        assert result["success"] is True

        # Wait for job2 to start and complete
        time.sleep(0.5)

        # Verify job2 completed
        job2_status = self.manager.get_job_status(job2_id, username="test_user")
        assert job2_status["status"] == "completed"

    def test_failed_job_frees_slot(self):
        """A failed job should free a slot for pending jobs."""
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        job2_started = threading.Event()

        def failing_job():
            raise Exception("Intentional failure")

        def success_job():
            job2_started.set()
            return {"status": "success"}

        # Submit failing job first
        job1_id = self.manager.submit_job(
            "failing_job", failing_job, submitter_username="test_user"
        )

        # Wait a moment for job1 to fail
        time.sleep(0.2)

        # Submit second job
        job2_id = self.manager.submit_job(
            "success_job", success_job, submitter_username="test_user"
        )

        # Wait for job2 to complete
        job2_started.wait(timeout=1.0)
        time.sleep(0.2)

        # Verify job1 failed
        job1_status = self.manager.get_job_status(job1_id, username="test_user")
        assert job1_status["status"] == "failed"

        # Verify job2 completed (it got the slot after job1 failed)
        job2_status = self.manager.get_job_status(job2_id, username="test_user")
        assert job2_status["status"] == "completed"

    def test_shutdown_with_queued_jobs(self):
        """Shutdown should handle running jobs gracefully.

        Note: With semaphore-based concurrency limiting, when the running job
        is cancelled during shutdown, its semaphore slot is released, allowing
        queued jobs to proceed. Fast jobs may complete before shutdown finishes.
        This is correct behavior - shutdown cancels RUNNING jobs, and queued
        jobs that acquire a slot are allowed to run to completion.
        """
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1)
        self.manager = BackgroundJobManager(
            storage_path=str(self.job_storage_path),
            background_jobs_config=config,
        )

        started_event = threading.Event()

        def blocking_job():
            started_event.set()
            time.sleep(10.0)
            return {"status": "success"}

        def waiting_job():
            return {"status": "success"}

        # Submit jobs
        job1_id = self.manager.submit_job(
            "blocking", blocking_job, submitter_username="test_user"
        )
        started_event.wait(timeout=1.0)

        job2_id = self.manager.submit_job(
            "waiting1", waiting_job, submitter_username="test_user"
        )
        job3_id = self.manager.submit_job(
            "waiting2", waiting_job, submitter_username="test_user"
        )

        # Shutdown cancels running jobs
        self.manager.shutdown()

        # Verify the running job was cancelled
        with self.manager._lock:
            job1 = self.manager.jobs.get(job1_id)
            job2 = self.manager.jobs.get(job2_id)
            job3 = self.manager.jobs.get(job3_id)

            # Running job should be cancelled
            assert job1.status == JobStatus.CANCELLED

            # With semaphore-based concurrency, when job1 is cancelled it releases
            # its slot, allowing waiting jobs to acquire it and run. Since waiting
            # jobs are fast, they may complete before we check. This is valid behavior.
            # Acceptable states: CANCELLED (if shutdown caught them), COMPLETED (if
            # they acquired slot and finished), or PENDING (if still waiting).
            valid_states = [JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.PENDING]
            assert job2.status in valid_states, f"job2 status: {job2.status}"
            assert job3.status in valid_states, f"job3 status: {job3.status}"

        # Prevent double shutdown
        self.manager = None
