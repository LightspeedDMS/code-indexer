"""
Unit tests for JobTracker.is_cancelled (Issue 1 from Codex review of Bug #853).

Covered scenarios:
1. is_cancelled returns False for a running non-cancelled job
2. is_cancelled returns False for an unknown job_id
3. is_cancelled reads DB directly, bypassing in-memory cache
"""

import os
import shutil
import sqlite3
import tempfile

from code_indexer.server.services.job_tracker import JobTracker


def _make_db_with_background_jobs_table() -> str:
    """Create a temp SQLite DB with the background_jobs schema. Returns db path."""
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    with sqlite3.connect(db_path) as conn:
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
    return db_path


class TestJobTrackerIsCancelled:
    """
    Issue 1: JobTracker.is_cancelled(job_id) -> bool must read the DB cancelled
    column DIRECTLY, bypassing in-memory cache.

    After BackgroundJobManager writes cancelled=1 to the DB row (without touching
    JobTracker's in-memory state), is_cancelled must return True so the scheduler
    thread can observe the cancellation.
    """

    def setup_method(self):
        self.db_path = _make_db_with_background_jobs_table()

    def teardown_method(self):
        db_dir = os.path.dirname(self.db_path)
        shutil.rmtree(db_dir, ignore_errors=True)

    def test_is_cancelled_returns_false_for_running_job_not_cancelled_in_db(self):
        """is_cancelled returns False when cancelled=0 in DB."""
        tracker = JobTracker(self.db_path)
        tracker.register_job("job-ic-001", "lifecycle_backfill", "system")
        tracker.update_status("job-ic-001", status="running")

        result = tracker.is_cancelled("job-ic-001")

        assert result is False, (
            f"is_cancelled must return False for running non-cancelled job, got {result}"
        )

    def test_is_cancelled_returns_false_for_unknown_job(self):
        """is_cancelled returns False for a job_id that does not exist."""
        tracker = JobTracker(self.db_path)

        result = tracker.is_cancelled("nonexistent-job-id")

        assert result is False, (
            f"is_cancelled must return False for unknown job, got {result}"
        )

    def test_is_cancelled_reads_db_directly_bypassing_in_memory_cache(self):
        """
        Core Issue 1 test: is_cancelled must read the DB cancelled column directly.

        Scenario:
        1. Register job — enters _active_jobs (status=running, cancelled not set)
        2. Directly write cancelled=1 to the DB row, bypassing JobTracker's memory
        3. is_cancelled must return True (reads DB) even though in-memory job unchanged
        """
        tracker = JobTracker(self.db_path)
        tracker.register_job("job-ic-002", "lifecycle_backfill", "system")
        tracker.update_status("job-ic-002", status="running")

        # Confirm job is in memory with status=running
        with tracker._lock:
            in_memory_job = tracker._active_jobs.get("job-ic-002")
        assert in_memory_job is not None, "Job must be in active_jobs dict"
        assert in_memory_job.status == "running"

        # Directly write cancelled=1 to DB, simulating BackgroundJobManager cancel path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE background_jobs SET cancelled = 1 WHERE job_id = ?",
                ("job-ic-002",),
            )

        # is_cancelled must read DB directly and return True
        result = tracker.is_cancelled("job-ic-002")

        assert result is True, (
            "is_cancelled must return True after cancelled=1 written to DB directly. "
            f"Got {result!r}. This indicates is_cancelled reads in-memory cache "
            "(wrong) instead of querying the DB cancelled column directly."
        )
