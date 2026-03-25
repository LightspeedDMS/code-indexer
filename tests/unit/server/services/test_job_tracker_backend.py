"""
Tests for JobTracker storage_backend delegation (Story #521).

TDD approach: tests written BEFORE implementation.

Covers:
- AC1: JobTracker accepts optional storage_backend parameter
- AC2: When storage_backend is provided, register_job delegates save to backend
- AC3: update_status/complete_job/fail_job delegate to backend.update_job
- AC4: get_job falls back to backend.get_job when job not in memory
- AC5: cleanup_orphaned_jobs_on_startup delegates to backend
- AC6: cleanup_old_jobs delegates to backend
- AC7: get_recent_jobs uses backend.list_jobs
- AC8: query_jobs uses backend.list_jobs with filters
- AC9: Without storage_backend, existing SQLite behaviour is unchanged
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_schema(db_path: str) -> None:
    """Create the background_jobs table (all columns) in the given SQLite DB."""
    conn = sqlite3.connect(db_path)
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
        metadata TEXT,
        current_phase TEXT,
        phase_detail TEXT
    )"""
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Temp SQLite DB path with background_jobs schema."""
    db = tmp_path / "test.db"
    _make_schema(str(db))
    return str(db)


@pytest.fixture
def backend(db_path):
    """Real BackgroundJobsSqliteBackend connected to temp DB."""
    return BackgroundJobsSqliteBackend(db_path)


@pytest.fixture
def tracker_with_backend(db_path, backend):
    """JobTracker using storage_backend delegation."""
    return JobTracker(db_path, storage_backend=backend)


@pytest.fixture
def tracker_without_backend(db_path):
    """JobTracker using legacy SQLite direct mode."""
    return JobTracker(db_path)


# ---------------------------------------------------------------------------
# AC1: Constructor accepts storage_backend parameter
# ---------------------------------------------------------------------------


class TestJobTrackerConstructor:
    """JobTracker must accept an optional storage_backend parameter."""

    def test_constructor_accepts_storage_backend(self, db_path, backend):
        """JobTracker(db_path, storage_backend=...) must not raise."""
        tracker = JobTracker(db_path, storage_backend=backend)
        assert tracker is not None

    def test_constructor_without_storage_backend_works(self, db_path):
        """JobTracker(db_path) with no storage_backend must still work."""
        tracker = JobTracker(db_path)
        assert tracker is not None

    def test_constructor_with_none_storage_backend_uses_sqlite(self, db_path):
        """JobTracker(db_path, storage_backend=None) should fall back to direct SQLite."""
        tracker = JobTracker(db_path, storage_backend=None)
        job = tracker.register_job("job-none-001", "test_op", "admin")
        assert job.job_id == "job-none-001"


# ---------------------------------------------------------------------------
# AC2: register_job delegates to backend.save_job
# ---------------------------------------------------------------------------


class TestRegisterJobDelegation:
    """register_job must persist through the storage backend."""

    def test_register_job_persists_via_backend(self, tracker_with_backend, backend):
        """
        register_job creates a TrackedJob and the backend can retrieve it.

        Given a JobTracker with a storage_backend
        When register_job is called
        Then backend.get_job returns the persisted job
        """
        tracker_with_backend.register_job("job-reg-001", "dep_map_analysis", "admin")

        row = backend.get_job("job-reg-001")
        assert row is not None
        assert row["job_id"] == "job-reg-001"
        assert row["operation_type"] == "dep_map_analysis"
        assert row["status"] == "pending"
        assert row["username"] == "admin"

    def test_register_job_with_repo_alias_persists(self, tracker_with_backend, backend):
        """register_job with repo_alias stores it via backend."""
        tracker_with_backend.register_job(
            "job-reg-002", "description_refresh", "bob", repo_alias="my-repo"
        )

        row = backend.get_job("job-reg-002")
        assert row is not None
        assert row["repo_alias"] == "my-repo"

    def test_register_job_with_metadata_persists(self, tracker_with_backend, backend):
        """register_job with metadata dict stores it via backend."""
        tracker_with_backend.register_job(
            "job-reg-003",
            "dep_map_analysis",
            "carol",
            metadata={"key": "value", "count": 42},
        )

        row = backend.get_job("job-reg-003")
        assert row is not None
        # metadata should be stored and retrievable (may be in 'metadata' key)
        # The backend.get_job dict should contain metadata
        # We check the job via tracker.get_job which goes through _dict_to_tracked_job
        job = tracker_with_backend.get_job("job-reg-003")
        assert job is not None
        assert job.metadata == {"key": "value", "count": 42}

    def test_register_job_adds_to_memory(self, tracker_with_backend):
        """register_job adds the job to in-memory dict regardless of backend mode."""
        tracker_with_backend.register_job("job-mem-001", "test_op", "admin")

        active = tracker_with_backend.get_active_jobs()
        assert any(j.job_id == "job-mem-001" for j in active)


# ---------------------------------------------------------------------------
# AC3: update_status/complete_job/fail_job delegate to backend.update_job
# ---------------------------------------------------------------------------


class TestUpdateDelegation:
    """State transitions must persist via backend.update_job."""

    def test_update_status_delegates_to_backend(self, tracker_with_backend, backend):
        """
        update_status persists status change via backend.

        Given a pending job
        When update_status is called with status='running'
        Then backend.get_job returns status='running'
        """
        tracker_with_backend.register_job("job-upd-001", "test_op", "admin")
        tracker_with_backend.update_status("job-upd-001", status="running")

        row = backend.get_job("job-upd-001")
        assert row is not None
        assert row["status"] == "running"

    def test_update_status_progress_delegates(self, tracker_with_backend, backend):
        """update_status with progress persists via backend."""
        tracker_with_backend.register_job("job-upd-002", "test_op", "admin")
        tracker_with_backend.update_status("job-upd-002", progress=50)

        row = backend.get_job("job-upd-002")
        assert row is not None
        assert row["progress"] == 50

    def test_complete_job_delegates_to_backend(self, tracker_with_backend, backend):
        """
        complete_job persists status='completed' via backend.

        Given a running job
        When complete_job is called
        Then backend.get_job returns status='completed' and completed_at is set
        """
        tracker_with_backend.register_job("job-comp-001", "test_op", "admin")
        tracker_with_backend.update_status("job-comp-001", status="running")
        tracker_with_backend.complete_job("job-comp-001")

        row = backend.get_job("job-comp-001")
        assert row is not None
        assert row["status"] == "completed"
        assert row["completed_at"] is not None

    def test_complete_job_with_result_delegates(self, tracker_with_backend, backend):
        """complete_job with result dict persists via backend."""
        tracker_with_backend.register_job("job-comp-002", "test_op", "admin")
        tracker_with_backend.complete_job("job-comp-002", result={"count": 5})

        row = backend.get_job("job-comp-002")
        assert row is not None
        assert row["result"] == {"count": 5}

    def test_fail_job_delegates_to_backend(self, tracker_with_backend, backend):
        """
        fail_job persists status='failed' and error via backend.

        Given a running job
        When fail_job is called with an error message
        Then backend.get_job returns status='failed' with error set
        """
        tracker_with_backend.register_job("job-fail-001", "test_op", "admin")
        tracker_with_backend.update_status("job-fail-001", status="running")
        tracker_with_backend.fail_job("job-fail-001", error="something went wrong")

        row = backend.get_job("job-fail-001")
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == "something went wrong"


# ---------------------------------------------------------------------------
# AC4: get_job falls back to backend when not in memory
# ---------------------------------------------------------------------------


class TestGetJobDelegation:
    """get_job must check memory first, then fall back to backend."""

    def test_get_job_from_memory(self, tracker_with_backend):
        """get_job returns in-memory job for active jobs."""
        tracker_with_backend.register_job("job-get-001", "test_op", "admin")

        job = tracker_with_backend.get_job("job-get-001")
        assert job is not None
        assert job.job_id == "job-get-001"

    def test_get_job_from_backend_after_completion(self, tracker_with_backend):
        """
        get_job returns job from backend after it is removed from memory.

        Given a completed job (no longer in memory)
        When get_job is called
        Then it returns the persisted TrackedJob via backend
        """
        tracker_with_backend.register_job("job-get-002", "test_op", "admin")
        tracker_with_backend.complete_job("job-get-002")

        # Job is no longer in active memory after completion
        active_ids = [j.job_id for j in tracker_with_backend.get_active_jobs()]
        assert "job-get-002" not in active_ids

        # But get_job should still find it via backend
        job = tracker_with_backend.get_job("job-get-002")
        assert job is not None
        assert job.job_id == "job-get-002"
        assert job.status == "completed"

    def test_get_job_returns_none_for_missing(self, tracker_with_backend):
        """get_job returns None for a job that does not exist."""
        job = tracker_with_backend.get_job("nonexistent-job-id")
        assert job is None

    def test_get_job_preserves_progress_info(self, tracker_with_backend):
        """get_job round-trip preserves progress_info field."""
        tracker_with_backend.register_job("job-pi-001", "test_op", "admin")
        tracker_with_backend.update_status(
            "job-pi-001", progress_info="Processing file 1/10"
        )
        tracker_with_backend.complete_job("job-pi-001")

        job = tracker_with_backend.get_job("job-pi-001")
        assert job is not None
        assert job.progress_info == "Processing file 1/10"


# ---------------------------------------------------------------------------
# AC5: cleanup_orphaned_jobs_on_startup delegates to backend
# ---------------------------------------------------------------------------


class TestCleanupOrphanedJobs:
    """cleanup_orphaned_jobs_on_startup must delegate to backend."""

    def test_cleanup_orphaned_jobs_marks_running_as_failed(self, db_path, backend):
        """
        cleanup_orphaned_jobs_on_startup marks orphaned running jobs as failed.

        Given jobs persisted with status running/pending
        When a new JobTracker is created (simulating restart) and cleanup is called
        Then those jobs are marked failed in the backend
        """
        # Pre-populate the backend with "orphaned" running job
        before = datetime.now(timezone.utc).isoformat()
        backend.save_job(
            job_id="job-orphan-001",
            operation_type="test_op",
            status="running",
            created_at=before,
            username="admin",
            progress=30,
        )

        # New tracker (simulates server restart) with backend
        new_tracker = JobTracker(db_path, storage_backend=backend)
        count = new_tracker.cleanup_orphaned_jobs_on_startup()

        assert count >= 1
        row = backend.get_job("job-orphan-001")
        assert row is not None
        assert row["status"] == "failed"

    def test_cleanup_orphaned_jobs_marks_pending_as_failed(self, db_path, backend):
        """Pending jobs are also marked as failed on startup cleanup."""
        before = datetime.now(timezone.utc).isoformat()
        backend.save_job(
            job_id="job-orphan-002",
            operation_type="test_op",
            status="pending",
            created_at=before,
            username="alice",
            progress=0,
        )

        new_tracker = JobTracker(db_path, storage_backend=backend)
        count = new_tracker.cleanup_orphaned_jobs_on_startup()

        assert count >= 1
        row = backend.get_job("job-orphan-002")
        assert row["status"] == "failed"

    def test_cleanup_does_not_touch_completed_jobs(self, db_path, backend):
        """cleanup_orphaned_jobs_on_startup leaves completed jobs intact."""
        before = datetime.now(timezone.utc).isoformat()
        backend.save_job(
            job_id="job-orphan-done",
            operation_type="test_op",
            status="completed",
            created_at=before,
            username="admin",
            progress=100,
        )

        new_tracker = JobTracker(db_path, storage_backend=backend)
        new_tracker.cleanup_orphaned_jobs_on_startup()

        row = backend.get_job("job-orphan-done")
        assert row["status"] == "completed"


# ---------------------------------------------------------------------------
# AC6: cleanup_old_jobs delegates to backend
# ---------------------------------------------------------------------------


class TestCleanupOldJobs:
    """cleanup_old_jobs must delegate to backend for deletion."""

    def test_cleanup_old_jobs_delegates(self, tracker_with_backend, backend):
        """
        cleanup_old_jobs removes old completed jobs via backend.

        Given a completed job with completed_at in the distant past
        When cleanup_old_jobs is called with max_age_hours=0 (delete all)
        Then the job is removed from the backend
        """
        # Register and immediately complete a job
        tracker_with_backend.register_job("job-old-001", "dep_map_analysis", "admin")
        tracker_with_backend.complete_job("job-old-001")

        # Backdating: we pass max_age_hours=0 to force deletion of all completed
        tracker_with_backend.cleanup_old_jobs("dep_map_analysis", max_age_hours=0)

        # The job should be gone from backend
        row = backend.get_job("job-old-001")
        assert row is None


# ---------------------------------------------------------------------------
# AC7: get_recent_jobs uses backend.list_jobs
# ---------------------------------------------------------------------------


class TestGetRecentJobs:
    """get_recent_jobs must merge active memory with backend history."""

    def test_get_recent_jobs_includes_active_jobs(self, tracker_with_backend):
        """Active (in-memory) jobs always appear in get_recent_jobs."""
        tracker_with_backend.register_job("job-recent-001", "test_op", "admin")

        recent = tracker_with_backend.get_recent_jobs(limit=10)
        ids = [j["job_id"] for j in recent]
        assert "job-recent-001" in ids

    def test_get_recent_jobs_includes_completed_jobs_from_backend(
        self, tracker_with_backend
    ):
        """Completed (historical) jobs from backend appear in get_recent_jobs."""
        tracker_with_backend.register_job("job-hist-001", "test_op", "admin")
        tracker_with_backend.complete_job("job-hist-001")

        recent = tracker_with_backend.get_recent_jobs(limit=10, time_filter="all")
        ids = [j["job_id"] for j in recent]
        assert "job-hist-001" in ids


# ---------------------------------------------------------------------------
# AC8: query_jobs uses backend.list_jobs with filters
# ---------------------------------------------------------------------------


class TestQueryJobs:
    """query_jobs must use backend when available."""

    def test_query_jobs_by_operation_type(self, tracker_with_backend):
        """query_jobs filters by operation_type via backend."""
        tracker_with_backend.register_job("job-qry-001", "dep_map_analysis", "admin")
        tracker_with_backend.register_job("job-qry-002", "description_refresh", "admin")
        tracker_with_backend.complete_job("job-qry-001")
        tracker_with_backend.complete_job("job-qry-002")

        results = tracker_with_backend.query_jobs(operation_type="dep_map_analysis")
        op_types = {r["operation_type"] for r in results}
        assert "dep_map_analysis" in op_types
        assert "description_refresh" not in op_types

    def test_query_jobs_by_status(self, tracker_with_backend):
        """query_jobs filters by status via backend."""
        tracker_with_backend.register_job("job-qry-003", "test_op", "admin")
        tracker_with_backend.complete_job("job-qry-003")
        tracker_with_backend.register_job("job-qry-004", "test_op", "admin")
        tracker_with_backend.fail_job("job-qry-004", error="oops")

        completed = tracker_with_backend.query_jobs(status="completed")
        statuses = {r["status"] for r in completed}
        assert "completed" in statuses
        assert "failed" not in statuses


# ---------------------------------------------------------------------------
# AC9: Without storage_backend, existing SQLite behaviour is unchanged
# ---------------------------------------------------------------------------


class TestLegacySQLiteMode:
    """Without storage_backend, all existing behaviour must be preserved."""

    def test_legacy_register_and_get(self, tracker_without_backend, db_path):
        """register_job and get_job work in legacy mode."""
        tracker_without_backend.register_job("job-leg-001", "test_op", "admin")
        job = tracker_without_backend.get_job("job-leg-001")
        assert job is not None
        assert job.job_id == "job-leg-001"

    def test_legacy_complete_job(self, tracker_without_backend):
        """complete_job works in legacy direct-SQLite mode."""
        tracker_without_backend.register_job("job-leg-002", "test_op", "admin")
        tracker_without_backend.complete_job("job-leg-002")

        job = tracker_without_backend.get_job("job-leg-002")
        assert job is not None
        assert job.status == "completed"

    def test_legacy_cleanup_orphaned(self, db_path, tracker_without_backend):
        """cleanup_orphaned_jobs_on_startup works in legacy mode."""
        # Pre-insert an orphaned job directly via SQLite
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO background_jobs
               (job_id, operation_type, status, created_at, username, progress,
                is_admin, cancelled, resolution_attempts)
               VALUES ('orphan-leg', 'test_op', 'running',
                       '2020-01-01T00:00:00+00:00', 'admin', 0, 0, 0, 0)"""
        )
        conn.commit()
        conn.close()

        new_tracker = JobTracker(db_path)
        count = new_tracker.cleanup_orphaned_jobs_on_startup()
        assert count >= 1
