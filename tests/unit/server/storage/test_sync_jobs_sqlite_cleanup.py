"""
Unit tests for SyncJobsSqliteBackend.cleanup_orphaned_jobs_on_startup().

TDD: Tests written FIRST before production code exists.
Bug #436: Orphaned jobs persist as "running" after server restart.

Fix 1: SyncJobsSqliteBackend needs cleanup_orphaned_jobs_on_startup() method
to mirror BackgroundJobsSqliteBackend.cleanup_orphaned_jobs_on_startup().
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return path to a freshly initialized test database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    path = tmp_path / "test_sync_cleanup.db"
    schema = DatabaseSchema(str(path))
    schema.initialize_database()
    return str(path)


@pytest.fixture
def backend(db_path: str) -> Generator:
    """Create a SyncJobsSqliteBackend with initialized database."""
    from code_indexer.server.storage.sqlite_backends import SyncJobsSqliteBackend

    b = SyncJobsSqliteBackend(db_path)
    yield b
    b.close()


def _insert_sync_job(db_path: str, job_id: str, status: str) -> None:
    """Insert a sync job directly into the DB to set up test state."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO sync_jobs
           (job_id, username, user_alias, job_type, status, created_at, progress)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, "testuser", "Test User", "sync", status, now, 0),
    )
    conn.commit()
    conn.close()


def _get_job_status(db_path: str, job_id: str) -> dict:
    """Read status and error_message for a job directly from DB."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT status, error_message, completed_at FROM sync_jobs WHERE job_id = ?",
        (job_id,),
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, f"Job {job_id!r} not found in DB"
    return {"status": row[0], "error_message": row[1], "completed_at": row[2]}


class TestCleanupOrphanedJobsOnStartup:
    """Tests for SyncJobsSqliteBackend.cleanup_orphaned_jobs_on_startup()."""

    def test_running_job_marked_as_failed(self, backend, db_path: str) -> None:
        """A job with status='running' must be marked 'failed' on cleanup."""
        _insert_sync_job(db_path, "job-running-1", "running")

        count = backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-running-1")
        assert row["status"] == "failed"
        assert count == 1

    def test_pending_job_marked_as_failed(self, backend, db_path: str) -> None:
        """A job with status='pending' must be marked 'failed' on cleanup."""
        _insert_sync_job(db_path, "job-pending-1", "pending")

        count = backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-pending-1")
        assert row["status"] == "failed"
        assert count == 1

    def test_completed_job_not_touched(self, backend, db_path: str) -> None:
        """A job with status='completed' must NOT be modified."""
        _insert_sync_job(db_path, "job-completed-1", "completed")

        backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-completed-1")
        assert row["status"] == "completed"

    def test_failed_job_not_touched(self, backend, db_path: str) -> None:
        """A job with status='failed' must NOT be modified."""
        _insert_sync_job(db_path, "job-failed-1", "failed")

        backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-failed-1")
        assert row["status"] == "failed"

    def test_returns_correct_count_multiple_orphans(
        self, backend, db_path: str
    ) -> None:
        """Count returned must equal the number of jobs actually cleaned up."""
        _insert_sync_job(db_path, "job-r1", "running")
        _insert_sync_job(db_path, "job-r2", "running")
        _insert_sync_job(db_path, "job-p1", "pending")
        _insert_sync_job(db_path, "job-c1", "completed")  # not touched

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 3  # 2 running + 1 pending

    def test_returns_zero_when_no_orphans(self, backend, db_path: str) -> None:
        """Returns 0 when there are no running or pending jobs."""
        _insert_sync_job(db_path, "job-done", "completed")

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 0

    def test_returns_zero_on_empty_table(self, backend) -> None:
        """Returns 0 when the sync_jobs table is completely empty."""
        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 0

    def test_error_message_set_to_restart_reason(self, backend, db_path: str) -> None:
        """Failed orphans must have an error_message explaining why."""
        _insert_sync_job(db_path, "job-orphan", "running")

        backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-orphan")
        assert row["error_message"] is not None
        assert (
            "restart" in row["error_message"].lower()
            or "interrupted" in row["error_message"].lower()
        )

    def test_completed_at_set_for_orphans(self, backend, db_path: str) -> None:
        """Orphans must have completed_at set when marked failed."""
        _insert_sync_job(db_path, "job-orphan-2", "running")

        backend.cleanup_orphaned_jobs_on_startup()

        row = _get_job_status(db_path, "job-orphan-2")
        assert row["completed_at"] is not None

    def test_mixed_statuses_only_orphans_cleaned(self, backend, db_path: str) -> None:
        """Only running/pending jobs are cleaned; completed and failed are untouched."""
        _insert_sync_job(db_path, "job-run", "running")
        _insert_sync_job(db_path, "job-pend", "pending")
        _insert_sync_job(db_path, "job-comp", "completed")
        _insert_sync_job(db_path, "job-fail", "failed")

        count = backend.cleanup_orphaned_jobs_on_startup()

        assert count == 2
        assert _get_job_status(db_path, "job-run")["status"] == "failed"
        assert _get_job_status(db_path, "job-pend")["status"] == "failed"
        assert _get_job_status(db_path, "job-comp")["status"] == "completed"
        assert _get_job_status(db_path, "job-fail")["status"] == "failed"
