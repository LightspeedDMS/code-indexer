"""
Unit tests for SyncJobsSqliteBackend cleanup methods.

TDD: Tests written FIRST before production code exists.
Bug #436: Orphaned jobs persist as "running" after server restart.
Bug #1068: sync_jobs PG retention used created_at instead of completed_at
           and only matched status='completed' (missing 'failed').

Fix 1: SyncJobsSqliteBackend needs cleanup_orphaned_jobs_on_startup() method
to mirror BackgroundJobsSqliteBackend.cleanup_orphaned_jobs_on_startup().

Fix 2: Real-SQLite straddle tests prove cleanup_old_completed() deletes by
completed_at (not created_at) and only touches completed/failed rows.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

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


@pytest.mark.slow
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


def _insert_sync_job_with_timestamps(
    db_path: str,
    job_id: str,
    status: str,
    created_at: str,
    completed_at: Optional[str],
) -> None:
    """Insert a sync job with explicit created_at and completed_at timestamps."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO sync_jobs
           (job_id, username, user_alias, job_type, status, created_at,
            completed_at, progress)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, "testuser", "Test User", "sync", status, created_at, completed_at, 0),
    )
    conn.commit()
    conn.close()


def _job_exists(db_path: str, job_id: str) -> bool:
    """Return True if job_id exists in sync_jobs table."""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM sync_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    return row is not None


@pytest.mark.slow
class TestCleanupOldCompletedStraddleCutoff:
    """
    Real-SQLite straddle tests for SyncJobsSqliteBackend.cleanup_old_completed().

    Bug #1068: proves that retention is governed by completed_at (not created_at)
    and that only status IN ('completed', 'failed') rows are eligible for deletion.

    These tests seed rows that straddle the cutoff boundary and verify exactly
    which rows survive — no mocking; all assertions are via real SQLite queries.
    PG==SQLite equivalence at the SQL level is verified by TestCleanupOldCompleted
    in test_sync_jobs_postgres.py (SQL-assertion tests confirm PG uses the same
    completed_at / status IN filter as SQLite; real-PG row-equivalence is covered
    by the server-fast and e2e test suites which run against a live PG cluster).
    """

    def test_completed_recently_created_long_ago_is_kept(
        self, backend, db_path: str
    ) -> None:
        """
        A job created long ago but completed AFTER the cutoff must be KEPT.

        Scenario: cutoff = 100 hours ago.
          - created_at = 200 hours ago  (old creation — would be deleted if
                                         retention were by created_at)
          - completed_at = 1 hour ago   (recent completion — must be KEPT)
          - status = 'completed'

        Bug #1068: if retention were by created_at, this row would be wrongly
        deleted; correct behaviour is to keep it until completed_at crosses
        the cutoff.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=100)).isoformat()
        created_long_ago = (now - timedelta(hours=200)).isoformat()
        completed_recently = (now - timedelta(hours=1)).isoformat()

        _insert_sync_job_with_timestamps(
            db_path,
            "keep-completed-recent",
            status="completed",
            created_at=created_long_ago,
            completed_at=completed_recently,
        )

        # cutoff_iso = 100 hours ago; completed_at = 1 hour ago → KEEP
        deleted = backend.cleanup_old_completed(cutoff_iso=cutoff)

        assert deleted == 0, (
            f"Row completed recently must be KEPT (deleted={deleted}). "
            "Retention must use completed_at, not created_at (Bug #1068)."
        )
        assert _job_exists(db_path, "keep-completed-recent"), (
            "Row was wrongly deleted. completed_at is within retention window."
        )

    def test_completed_long_ago_is_deleted(self, backend, db_path: str) -> None:
        """
        A completed job whose completed_at is older than the cutoff must be DELETED.

        Scenario: cutoff = 10 hours ago.
          - created_at = 200 hours ago
          - completed_at = 50 hours ago  (old completion — must be DELETED)
          - status = 'completed'
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=10)).isoformat()
        created_long_ago = (now - timedelta(hours=200)).isoformat()
        completed_long_ago = (now - timedelta(hours=50)).isoformat()

        _insert_sync_job_with_timestamps(
            db_path,
            "delete-completed-old",
            status="completed",
            created_at=created_long_ago,
            completed_at=completed_long_ago,
        )

        deleted = backend.cleanup_old_completed(cutoff_iso=cutoff)

        assert deleted == 1, (
            f"Row with old completed_at must be DELETED (deleted={deleted}). "
            "Bug #1068: completed_at < cutoff AND status='completed' → delete."
        )
        assert not _job_exists(db_path, "delete-completed-old"), (
            "Row must have been deleted (completed_at is beyond retention window)."
        )

    def test_failed_long_ago_is_deleted(self, backend, db_path: str) -> None:
        """
        A failed job whose completed_at is older than the cutoff must be DELETED.

        Bug #1068: original PG query only matched status='completed'.  SQLite
        matches status IN ('completed', 'failed') — both must be deleted.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=10)).isoformat()
        created_long_ago = (now - timedelta(hours=200)).isoformat()
        completed_long_ago = (now - timedelta(hours=50)).isoformat()

        _insert_sync_job_with_timestamps(
            db_path,
            "delete-failed-old",
            status="failed",
            created_at=created_long_ago,
            completed_at=completed_long_ago,
        )

        deleted = backend.cleanup_old_completed(cutoff_iso=cutoff)

        assert deleted == 1, (
            f"Failed row with old completed_at must be DELETED (deleted={deleted}). "
            "Bug #1068: status='failed' must be included in retention filter."
        )
        assert not _job_exists(db_path, "delete-failed-old"), (
            "Failed row with old completed_at must have been deleted."
        )

    def test_wrong_status_row_is_kept_regardless_of_age(
        self, backend, db_path: str
    ) -> None:
        """
        A row with status='running' or 'pending' must NEVER be deleted by
        cleanup_old_completed(), even if both created_at and completed_at
        (if set) are older than the cutoff.

        The retention filter is strictly: status IN ('completed', 'failed').
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=10)).isoformat()
        created_long_ago = (now - timedelta(hours=200)).isoformat()
        completed_long_ago = (now - timedelta(hours=50)).isoformat()

        _insert_sync_job_with_timestamps(
            db_path,
            "keep-running",
            status="running",
            created_at=created_long_ago,
            completed_at=completed_long_ago,  # has a timestamp but wrong status
        )
        _insert_sync_job_with_timestamps(
            db_path,
            "keep-pending",
            status="pending",
            created_at=created_long_ago,
            completed_at=None,
        )

        deleted = backend.cleanup_old_completed(cutoff_iso=cutoff)

        assert deleted == 0, (
            f"Rows with non-terminal status must NEVER be deleted "
            f"by cleanup_old_completed (deleted={deleted})."
        )
        assert _job_exists(db_path, "keep-running"), "Running row must have been kept."
        assert _job_exists(db_path, "keep-pending"), "Pending row must have been kept."
