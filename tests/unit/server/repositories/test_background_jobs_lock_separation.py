"""
Unit tests for BackgroundJobManager lock separation (Story #267, Component 4).

Tests that SQLite persistence happens outside the memory lock, so dashboard reads
are never blocked by I/O operations.
"""

import tempfile
import time
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
    BackgroundJob,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


class TestLockSeparation:
    """Test persistence outside lock (Component 4)."""

    def setup_method(self):
        """Setup test environment with SQLite backend."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        # Initialize database schema (creates background_jobs table)
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
            ),
        )

    def teardown_method(self):
        """Clean up test environment."""
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_persist_outside_lock_does_not_block_reads(self):
        """Component 4: Dashboard reads should not be blocked during SQLite I/O.

        Use a slow SQLite backend to simulate I/O latency, then verify that
        get_active_job_count() returns within a short time window even while
        persistence is in progress.
        """
        # Add a running job to memory
        self.manager.jobs["running-1"] = BackgroundJob(
            job_id="running-1",
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )

        # Inject a slow save to simulate I/O latency
        original_save = self.manager._sqlite_backend.save_job
        save_started = threading.Event()
        save_can_finish = threading.Event()

        def slow_save(**kwargs):
            save_started.set()
            save_can_finish.wait(timeout=5.0)
            return original_save(**kwargs)

        self.manager._sqlite_backend.save_job = slow_save

        # Start persistence in a background thread (simulating a job submit)
        persist_thread = threading.Thread(
            target=self.manager._persist_jobs,
            kwargs={"job_id": "running-1"},
        )
        persist_thread.start()

        # Wait for save to begin (it's now holding onto SQLite I/O)
        save_started.wait(timeout=2.0)
        assert save_started.is_set(), "Save should have started"

        # Now try to read from memory - this should NOT be blocked
        read_completed = threading.Event()
        read_result = [None]
        read_duration = [0.0]

        def read_active_count():
            start = time.monotonic()
            read_result[0] = self.manager.get_active_job_count()
            read_duration[0] = time.monotonic() - start
            read_completed.set()

        read_thread = threading.Thread(target=read_active_count)
        read_thread.start()

        # The read should complete quickly (under 1 second)
        read_completed.wait(timeout=2.0)
        assert read_completed.is_set(), "Read should have completed while persist was in progress"
        assert read_result[0] == 1, "Should see 1 running job"
        assert read_duration[0] < 1.0, (
            f"Read took {read_duration[0]:.3f}s, should be under 1s (not blocked by persist)"
        )

        # Clean up
        save_can_finish.set()
        persist_thread.join(timeout=5.0)
        read_thread.join(timeout=1.0)

    def test_concurrent_job_completions_no_deadlock(self):
        """Component 4: Multiple jobs completing simultaneously should not deadlock.

        Complete 10 jobs from 10 threads. All should complete within 5 seconds.
        """
        job_ids = []

        # Create 10 jobs in memory and DB
        for i in range(10):
            jid = f"concurrent-{i}"
            job_ids.append(jid)
            self.manager.jobs[jid] = BackgroundJob(
                job_id=jid,
                operation_type="test_op",
                status=JobStatus.RUNNING,
                created_at=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
                completed_at=None,
                result=None,
                error=None,
                progress=50,
                username="testuser",
            )
            self.manager._sqlite_backend.save_job(
                job_id=jid,
                operation_type="test_op",
                status="running",
                created_at=datetime.now(timezone.utc).isoformat(),
                username="testuser",
                progress=50,
            )

        # Complete all 10 jobs concurrently
        errors = []

        def complete_job(job_id):
            try:
                with self.manager._lock:
                    job = self.manager.jobs[job_id]
                    job.status = JobStatus.COMPLETED
                    job.completed_at = datetime.now(timezone.utc)
                    job.progress = 100
                # Persist outside lock
                self.manager._persist_jobs(job_id=job_id)
            except Exception as e:
                errors.append((job_id, str(e)))

        threads = []
        for jid in job_ids:
            t = threading.Thread(target=complete_job, args=(jid,))
            threads.append(t)

        # Start all threads at approximately the same time
        for t in threads:
            t.start()

        # Wait for all to complete (should not deadlock)
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), f"Thread {t.name} is still alive - possible deadlock"

        assert len(errors) == 0, f"Errors during concurrent completion: {errors}"

        # Verify all jobs are completed in memory
        for jid in job_ids:
            assert self.manager.jobs[jid].status == JobStatus.COMPLETED

    def test_memory_state_correct_after_persist_failure(self):
        """Component 4: In-memory state should be correct even if SQLite write fails.

        If the SQLite write fails, the in-memory job status should still reflect
        the updated state (COMPLETED), and a warning should be logged.
        """
        # Add a job
        self.manager.jobs["fail-persist"] = BackgroundJob(
            job_id="fail-persist",
            operation_type="test_op",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            result=None,
            error=None,
            progress=50,
            username="testuser",
        )

        # Make the SQLite backend raise an error on update
        def failing_update(job_id, **kwargs):
            raise Exception("Simulated SQLite failure")

        def failing_save(**kwargs):
            raise Exception("Simulated SQLite failure")

        self.manager._sqlite_backend.update_job = failing_update
        self.manager._sqlite_backend.save_job = failing_save

        # Update the job status in memory
        with self.manager._lock:
            job = self.manager.jobs["fail-persist"]
            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.now(timezone.utc)
            job.progress = 100

        # Persist should fail but not raise (it logs the error)
        self.manager._persist_jobs(job_id="fail-persist")

        # Memory state should still be COMPLETED (not rolled back)
        assert self.manager.jobs["fail-persist"].status == JobStatus.COMPLETED
        assert self.manager.jobs["fail-persist"].progress == 100
