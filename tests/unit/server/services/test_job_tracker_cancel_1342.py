"""
Unit tests for JobTracker.cancel_job (Bug #1342 symptom 2).

Root cause (from the bug report): BackgroundJobManager writes a terminal
CANCELLED status to the shared background_jobs DB row and evicts its own
in-memory dict entry, but never tells JobTracker the job is terminal.
JobTracker's in-memory `_active_jobs[job_id]` -- last set to "running" at job
start -- is therefore never removed, and the dashboard's recent-activity feed
(which always includes `_active_jobs` regardless of the time filter) shows a
cancelled job as a permanently "running" zombie until server restart.

cancel_job(job_id) is the missing terminal transition: it mirrors fail_job's
contract (status set, completed_at set, persisted, popped from
_active_jobs) but for "cancelled" instead of "failed", so the zombie entry
is removed the moment cancellation is finalized -- no restart required.

Uses the real SQLite-backed `tracker`/`db_path` fixtures from
tests/unit/server/services/conftest.py (no mocks) -- same convention as the
existing test_job_tracker_lifecycle.py suite for complete_job/fail_job.
"""

import sqlite3
from datetime import datetime, timezone


class TestJobTrackerCancel:
    """Tests for cancel_job (Bug #1342)."""

    def test_cancel_job_sets_cancelled_status(self, tracker):
        tracker.register_job("job-cancel-001", "activate_repository", "testuser")
        tracker.update_status("job-cancel-001", status="running")
        tracker.cancel_job("job-cancel-001")

        job = tracker.get_job("job-cancel-001")
        assert job is not None
        assert job.status == "cancelled"

    def test_cancel_job_sets_completed_at(self, tracker):
        before = datetime.now(timezone.utc)
        tracker.register_job("job-cancelAt-001", "activate_repository", "testuser")
        tracker.update_status("job-cancelAt-001", status="running")
        tracker.cancel_job("job-cancelAt-001")
        after = datetime.now(timezone.utc)

        job = tracker.get_job("job-cancelAt-001")
        assert job is not None
        assert job.completed_at is not None
        assert before <= job.completed_at <= after

    def test_cancel_job_removes_from_active_jobs(self, tracker):
        """The whole point of Bug #1342 symptom 2: a cancelled job must NOT
        remain in _active_jobs, or the dashboard shows it as a running
        zombie forever."""
        tracker.register_job("job-cancelMem-001", "activate_repository", "testuser")
        tracker.update_status("job-cancelMem-001", status="running")
        tracker.cancel_job("job-cancelMem-001")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-cancelMem-001" not in active_ids

    def test_cancel_job_persists_to_sqlite(self, tracker, db_path):
        tracker.register_job("job-sqlCancel-001", "activate_repository", "testuser")
        tracker.update_status("job-sqlCancel-001", status="running")
        tracker.cancel_job("job-sqlCancel-001")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?",
            ("job-sqlCancel-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "cancelled"

    def test_cancel_job_ignores_missing_and_absent_db_row(self, tracker):
        """cancel_job silently no-ops when the job_id is unknown both in
        memory and in the DB (mirrors fail_job_ignores_missing)."""
        tracker.cancel_job("nonexistent-job")

    def test_cancel_job_absent_from_memory_but_db_row_still_running_forces_terminal(
        self, tracker, db_path
    ):
        """Zombie-prevention fallback (mirrors the complete_job/fail_job
        Bug #1258 behavior): if the in-memory entry was lost but the DB row
        is stuck non-terminal, cancel_job must force it to 'cancelled'
        rather than leaving a permanent zombie."""
        tracker.register_job("job-forced-cancel-001", "activate_repository", "admin")
        tracker.update_status("job-forced-cancel-001", status="running")

        # Simulate the in-memory entry being lost (e.g. a crash) while the
        # DB row remains "running".
        with tracker._lock:
            tracker._active_jobs.pop("job-forced-cancel-001", None)

        tracker.cancel_job("job-forced-cancel-001")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?",
            ("job-forced-cancel-001",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "cancelled"

    def test_cancel_job_already_cancelled_in_db_is_idempotent(self, tracker, db_path):
        """Benign double-dispatch: calling cancel_job twice (or after the
        row is already terminal) must not raise or corrupt state."""
        tracker.register_job("job-idempotent-001", "activate_repository", "admin")
        tracker.update_status("job-idempotent-001", status="running")
        tracker.cancel_job("job-idempotent-001")

        # Second call: job_id already popped from _active_jobs, DB row
        # already 'cancelled' -- must be a silent no-op, not an error.
        tracker.cancel_job("job-idempotent-001")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT status FROM background_jobs WHERE job_id = ?",
            ("job-idempotent-001",),
        )
        row = cursor.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "cancelled"
