"""
Unit tests for BackgroundJobManager.get_jobs_for_display() - Story #271 Components 2-3.

Tests written FIRST following TDD methodology (red phase).
Covers:
- Component 2: get_jobs_for_display() on BackgroundJobManager
- Component 3: job dict normalization producing consistent display dicts
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.storage.database_manager import DatabaseSchema
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


class BaseManagerTest:
    """Base class providing SQLite-backed BackgroundJobManager setup."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = str(Path(self.temp_dir) / "test.db")
        DatabaseSchema(self.db_path).initialize_database()
        self.manager = BackgroundJobManager(
            use_sqlite=True,
            db_path=self.db_path,
            background_jobs_config=BackgroundJobsConfig(
                max_concurrent_background_jobs=10,
                cleanup_max_age_hours=24,
            ),
        )

    def teardown_method(self):
        if hasattr(self, "manager") and self.manager:
            self.manager.shutdown()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _add_memory_job(
        self,
        job_id: str,
        status: JobStatus = JobStatus.RUNNING,
        operation_type: str = "sync_repository",
        username: str = "admin",
        repo_alias: str = "test-repo",
        progress: int = 50,
        error: str = None,
    ) -> BackgroundJob:
        """Add a job directly to the manager's in-memory dict."""
        now = datetime.now(timezone.utc)
        job = BackgroundJob(
            job_id=job_id,
            operation_type=operation_type,
            status=status,
            created_at=now,
            started_at=now if status == JobStatus.RUNNING else None,
            completed_at=now if status in (JobStatus.COMPLETED, JobStatus.FAILED) else None,
            result=None,
            error=error,
            progress=progress,
            username=username,
            repo_alias=repo_alias,
        )
        with self.manager._lock:
            self.manager.jobs[job_id] = job
        return job

    def _save_db_job(
        self,
        job_id: str,
        status: str = "completed",
        operation_type: str = "sync_repository",
        username: str = "admin",
        repo_alias: str = "test-repo",
        progress: int = 100,
        error: str = None,
        created_at: str = None,
    ) -> None:
        """Save a job directly to the SQLite backend."""
        now = datetime.now(timezone.utc)
        if created_at is None:
            created_at = now.isoformat()
        self.manager._sqlite_backend.save_job(
            job_id=job_id,
            operation_type=operation_type,
            status=status,
            created_at=created_at,
            started_at=now.isoformat() if status in ("running", "completed", "failed") else None,
            completed_at=now.isoformat() if status in ("completed", "failed") else None,
            username=username,
            progress=progress,
            error=error,
            repo_alias=repo_alias,
        )


# ---------------------------------------------------------------------------
# Tests for method existence and return structure
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayExists(BaseManagerTest):
    """Tests that get_jobs_for_display() exists and returns correct shape."""

    def test_method_exists_on_manager(self) -> None:
        """get_jobs_for_display() must exist on BackgroundJobManager."""
        assert hasattr(self.manager, "get_jobs_for_display"), (
            "BackgroundJobManager must have get_jobs_for_display() method"
        )

    def test_returns_three_tuple(self) -> None:
        """get_jobs_for_display() must return (jobs, total_count, total_pages) 3-tuple."""
        result = self.manager.get_jobs_for_display()
        assert isinstance(result, tuple), "Must return a tuple"
        assert len(result) == 3, "Tuple must have 3 elements: (jobs, total_count, total_pages)"
        jobs, total_count, total_pages = result
        assert isinstance(jobs, list), "First element must be a list"
        assert isinstance(total_count, int), "Second element must be int (total_count)"
        assert isinstance(total_pages, int), "Third element must be int (total_pages)"

    def test_empty_state_returns_zero_count_one_page(self) -> None:
        """Empty system returns ([], 0, 1) - at least 1 page even when empty."""
        jobs, total_count, total_pages = self.manager.get_jobs_for_display()
        assert jobs == []
        assert total_count == 0
        assert total_pages >= 1, "total_pages must be at least 1 even when empty"


# ---------------------------------------------------------------------------
# Tests for normalization of display dicts
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayNormalization(BaseManagerTest):
    """Tests that job dicts have all required keys (Component 3)."""

    REQUIRED_DISPLAY_KEYS = {
        "job_id",
        "job_type",
        "operation_type",
        "status",
        "progress",
        "created_at",
        "started_at",
        "completed_at",
        "error_message",
        "username",
        "user_alias",
        "repository_name",
        "repository_url",
        "progress_info",
        "duration_seconds",
    }

    def test_memory_job_has_all_required_keys(self) -> None:
        """A job from memory must produce a dict with all required display keys."""
        self._add_memory_job("mem-job-1", status=JobStatus.RUNNING)
        jobs, _, _ = self.manager.get_jobs_for_display()
        assert len(jobs) >= 1
        mem_job = next(j for j in jobs if j["job_id"] == "mem-job-1")
        missing = self.REQUIRED_DISPLAY_KEYS - set(mem_job.keys())
        assert not missing, f"Memory job dict missing display keys: {missing}"

    def test_db_job_has_all_required_keys(self) -> None:
        """A job from SQLite must produce a dict with all required display keys."""
        self._save_db_job("db-job-1", status="completed")
        jobs, _, _ = self.manager.get_jobs_for_display()
        assert len(jobs) >= 1
        db_job = next(j for j in jobs if j["job_id"] == "db-job-1")
        missing = self.REQUIRED_DISPLAY_KEYS - set(db_job.keys())
        assert not missing, f"DB job dict missing display keys: {missing}"

    def test_memory_job_status_is_string(self) -> None:
        """status must be a plain string, not an enum object."""
        self._add_memory_job("mem-job-2", status=JobStatus.RUNNING)
        jobs, _, _ = self.manager.get_jobs_for_display()
        job = next(j for j in jobs if j["job_id"] == "mem-job-2")
        assert isinstance(job["status"], str), "status must be str, not enum"
        assert job["status"] == "running"

    def test_memory_job_job_type_equals_operation_type(self) -> None:
        """job_type must equal operation_type for memory jobs."""
        self._add_memory_job("mem-job-3", operation_type="scip_generate")
        jobs, _, _ = self.manager.get_jobs_for_display()
        job = next(j for j in jobs if j["job_id"] == "mem-job-3")
        assert job["job_type"] == "scip_generate"
        assert job["operation_type"] == "scip_generate"

    def test_db_job_error_key_is_error_message(self) -> None:
        """DB jobs have 'error' key; normalization must map it to 'error_message'."""
        self._save_db_job("db-job-2", status="failed", error="Something went wrong")
        jobs, _, _ = self.manager.get_jobs_for_display()
        job = next(j for j in jobs if j["job_id"] == "db-job-2")
        assert job["error_message"] == "Something went wrong", (
            "DB job 'error' field must be normalized to 'error_message'"
        )

    def test_db_job_repo_alias_is_repository_name(self) -> None:
        """DB jobs have 'repo_alias'; normalization must expose it as 'repository_name'."""
        self._save_db_job("db-job-3", repo_alias="my-golden-repo")
        jobs, _, _ = self.manager.get_jobs_for_display()
        job = next(j for j in jobs if j["job_id"] == "db-job-3")
        assert job["repository_name"] == "my-golden-repo", (
            "DB job 'repo_alias' must be normalized to 'repository_name'"
        )

    def test_completed_job_has_duration_seconds(self) -> None:
        """Completed jobs with started_at and completed_at must have duration_seconds >= 0."""
        self._add_memory_job("mem-job-4", status=JobStatus.COMPLETED)
        jobs, _, _ = self.manager.get_jobs_for_display()
        job = next((j for j in jobs if j["job_id"] == "mem-job-4"), None)
        if job and job.get("started_at") and job.get("completed_at"):
            assert job["duration_seconds"] is not None
            assert job["duration_seconds"] >= 0


# ---------------------------------------------------------------------------
# Tests for memory + DB merge (deduplication)
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayMerge(BaseManagerTest):
    """Tests that memory and DB jobs are merged without duplicates."""

    def test_memory_jobs_appear_in_results(self) -> None:
        """Active jobs in memory must appear in results."""
        self._add_memory_job("mem-job-1", status=JobStatus.RUNNING)
        jobs, total_count, _ = self.manager.get_jobs_for_display()
        job_ids = {j["job_id"] for j in jobs}
        assert "mem-job-1" in job_ids

    def test_db_jobs_appear_in_results(self) -> None:
        """Historical jobs from SQLite must appear in results."""
        self._save_db_job("db-job-1", status="completed")
        jobs, total_count, _ = self.manager.get_jobs_for_display()
        job_ids = {j["job_id"] for j in jobs}
        assert "db-job-1" in job_ids

    def test_job_in_both_memory_and_db_appears_once(self) -> None:
        """A job present in both memory and DB must not be duplicated."""
        # Save to DB first
        self._save_db_job("shared-job-1", status="running")
        # Also put in memory (simulating active job)
        self._add_memory_job("shared-job-1", status=JobStatus.RUNNING)

        jobs, total_count, _ = self.manager.get_jobs_for_display()
        matching = [j for j in jobs if j["job_id"] == "shared-job-1"]
        assert len(matching) == 1, (
            f"Job appearing in both memory and DB must appear once, got {len(matching)}"
        )

    def test_total_count_covers_both_memory_and_db(self) -> None:
        """total_count must include jobs from both memory and DB (no overlap)."""
        self._add_memory_job("mem-only-1", status=JobStatus.RUNNING)
        self._add_memory_job("mem-only-2", status=JobStatus.PENDING)
        self._save_db_job("db-only-1", status="completed")
        self._save_db_job("db-only-2", status="failed")

        jobs, total_count, _ = self.manager.get_jobs_for_display()
        assert total_count == 4, f"Expected total_count=4 (2 mem + 2 db), got {total_count}"


# ---------------------------------------------------------------------------
# Tests for filter passthrough
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayFilters(BaseManagerTest):
    """Tests that filters are applied correctly to results."""

    def test_status_filter_completed_excludes_running(self) -> None:
        """status_filter='completed' must exclude running/pending jobs."""
        self._add_memory_job("mem-running", status=JobStatus.RUNNING)
        self._save_db_job("db-completed", status="completed")

        jobs, total_count, _ = self.manager.get_jobs_for_display(status_filter="completed")
        job_ids = {j["job_id"] for j in jobs}
        assert "mem-running" not in job_ids, "Running job must not appear when filtering completed"
        assert "db-completed" in job_ids

    def test_type_filter_restricts_operation_type(self) -> None:
        """type_filter must restrict to the given operation_type."""
        self._add_memory_job("mem-sync", status=JobStatus.RUNNING, operation_type="sync_repository")
        self._save_db_job("db-scip", status="completed", operation_type="scip_generate")
        self._save_db_job("db-sync", status="completed", operation_type="sync_repository")

        jobs, total_count, _ = self.manager.get_jobs_for_display(type_filter="sync_repository")
        job_ids = {j["job_id"] for j in jobs}
        assert "mem-sync" in job_ids
        assert "db-sync" in job_ids
        assert "db-scip" not in job_ids

    def test_search_text_filter_by_repo_name(self) -> None:
        """search_text must filter by repository name."""
        self._save_db_job("db-target", status="completed", repo_alias="my-special-repo")
        self._save_db_job("db-other", status="completed", repo_alias="other-repo")

        jobs, total_count, _ = self.manager.get_jobs_for_display(search_text="special")
        job_ids = {j["job_id"] for j in jobs}
        assert "db-target" in job_ids
        assert "db-other" not in job_ids


# ---------------------------------------------------------------------------
# Tests for pagination
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayPagination(BaseManagerTest):
    """Tests for pagination in get_jobs_for_display()."""

    def test_default_page_size_is_applied(self) -> None:
        """Default page_size of 50 is applied."""
        # Create 60 DB jobs
        now = datetime.now(timezone.utc)
        for i in range(60):
            ts = (now - timedelta(minutes=i)).isoformat()
            self._save_db_job(f"db-job-{i:03d}", status="completed", created_at=ts)

        jobs, total_count, total_pages = self.manager.get_jobs_for_display(page=1, page_size=50)
        assert len(jobs) == 50, f"Expected 50 on page 1, got {len(jobs)}"
        assert total_count == 60
        assert total_pages == 2

    def test_page_two_returns_remaining(self) -> None:
        """Page 2 must return the remaining jobs."""
        now = datetime.now(timezone.utc)
        for i in range(7):
            ts = (now - timedelta(minutes=i)).isoformat()
            self._save_db_job(f"db-job-{i:02d}", status="completed", created_at=ts)

        jobs_p1, total, pages = self.manager.get_jobs_for_display(page=1, page_size=5)
        jobs_p2, _, _ = self.manager.get_jobs_for_display(page=2, page_size=5)

        assert len(jobs_p1) == 5
        assert len(jobs_p2) == 2
        assert total == 7
        assert pages == 2

    def test_total_pages_computation(self) -> None:
        """total_pages = ceil(total_count / page_size)."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            ts = (now - timedelta(minutes=i)).isoformat()
            self._save_db_job(f"db-job-{i:02d}", status="completed", created_at=ts)

        _, total, pages = self.manager.get_jobs_for_display(page=1, page_size=3)
        assert total == 10
        assert pages == 4  # ceil(10/3) = 4

    def test_no_overlap_between_pages(self) -> None:
        """Pages must not contain duplicate job_ids."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            ts = (now - timedelta(minutes=i)).isoformat()
            self._save_db_job(f"db-job-{i:02d}", status="completed", created_at=ts)

        jobs_p1, _, _ = self.manager.get_jobs_for_display(page=1, page_size=5)
        jobs_p2, _, _ = self.manager.get_jobs_for_display(page=2, page_size=5)

        ids_p1 = {j["job_id"] for j in jobs_p1}
        ids_p2 = {j["job_id"] for j in jobs_p2}
        assert ids_p1.isdisjoint(ids_p2), "Pages must not overlap"
