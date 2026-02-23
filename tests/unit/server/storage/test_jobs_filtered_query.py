"""
Unit tests for BackgroundJobsSqliteBackend.list_jobs_filtered() - Story #271.

Tests written FIRST following TDD methodology (red phase).
The list_jobs_filtered() method does not yet exist - these tests will fail until implemented.

Covers acceptance criteria:
- AC2: Status filter shows only jobs of that status from database
- AC4: Type filter works at database level
- AC5: Text search queries the database (repo name, username)
- AC6: Pagination with accurate total counts
- AC7: Combined filters with pagination
- AC12: Empty state handling
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Return path to a temp SQLite database."""
    return str(tmp_path / "test.db")


@pytest.fixture
def backend(db_path: str):
    """Create a BackgroundJobsSqliteBackend with initialized database schema."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    schema = DatabaseSchema(db_path)
    schema.initialize_database()
    return BackgroundJobsSqliteBackend(db_path)


def _save_job(
    backend,
    job_id: str,
    status: str,
    operation_type: str = "sync_repository",
    username: str = "admin",
    repo_alias: str = "my-repo",
    created_at: str = None,
    started_at: str = None,
    completed_at: str = None,
    progress: int = 100,
    error: str = None,
) -> None:
    """Helper to save a job with sensible defaults."""
    now = datetime.now(timezone.utc)
    if created_at is None:
        created_at = now.isoformat()
    backend.save_job(
        job_id=job_id,
        operation_type=operation_type,
        status=status,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        username=username,
        progress=progress,
        error=error,
        repo_alias=repo_alias,
    )


class TestListJobsFilteredBasicStructure:
    """Tests that list_jobs_filtered() exists and returns correct data structure."""

    def test_method_exists_on_backend(self, backend) -> None:
        """list_jobs_filtered() must exist on BackgroundJobsSqliteBackend."""
        assert hasattr(backend, "list_jobs_filtered"), (
            "BackgroundJobsSqliteBackend must have list_jobs_filtered() method"
        )

    def test_returns_tuple_of_list_and_count(self, backend) -> None:
        """list_jobs_filtered() must return (List[Dict], int) tuple."""
        result = backend.list_jobs_filtered()
        assert isinstance(result, tuple), "Must return a tuple"
        assert len(result) == 2, "Tuple must have exactly 2 elements: (jobs, total_count)"
        jobs, total_count = result
        assert isinstance(jobs, list), "First element must be a list"
        assert isinstance(total_count, int), "Second element must be an int (total count)"

    def test_empty_database_returns_empty_list_and_zero_count(self, backend) -> None:
        """AC12: Empty state - no jobs returns ([], 0)."""
        jobs, total_count = backend.list_jobs_filtered()
        assert jobs == [], "Empty database should return empty list"
        assert total_count == 0, "Empty database should have total_count of 0"

    def test_job_dict_has_required_keys(self, backend) -> None:
        """Returned job dicts must have all keys needed for normalization."""
        _save_job(backend, "job-001", "completed", repo_alias="test-repo")
        jobs, _ = backend.list_jobs_filtered()
        assert len(jobs) == 1
        job = jobs[0]
        # Keys that must be present (same as _row_to_dict produces)
        required_keys = {
            "job_id", "operation_type", "status", "created_at",
            "started_at", "completed_at", "result", "error", "progress",
            "username", "is_admin", "cancelled", "repo_alias",
        }
        missing = required_keys - set(job.keys())
        assert not missing, f"Job dict missing keys: {missing}"


class TestListJobsFilteredByStatus:
    """Tests for status filter (AC2)."""

    def test_status_filter_completed_returns_only_completed(self, backend) -> None:
        """AC2: Status filter 'completed' returns only completed jobs."""
        _save_job(backend, "job-completed-1", "completed")
        _save_job(backend, "job-completed-2", "completed")
        _save_job(backend, "job-failed-1", "failed")
        _save_job(backend, "job-running-1", "running")

        jobs, total_count = backend.list_jobs_filtered(status="completed")
        assert len(jobs) == 2, f"Expected 2 completed jobs, got {len(jobs)}"
        assert total_count == 2, f"Expected total_count=2, got {total_count}"
        for job in jobs:
            assert job["status"] == "completed", f"Got non-completed job: {job['status']}"

    def test_status_filter_failed_returns_only_failed(self, backend) -> None:
        """AC2: Status filter 'failed' returns only failed jobs."""
        _save_job(backend, "job-completed-1", "completed")
        _save_job(backend, "job-failed-1", "failed")
        _save_job(backend, "job-failed-2", "failed")

        jobs, total_count = backend.list_jobs_filtered(status="failed")
        assert len(jobs) == 2, f"Expected 2 failed jobs, got {len(jobs)}"
        assert total_count == 2
        for job in jobs:
            assert job["status"] == "failed"

    def test_status_filter_running_returns_only_running(self, backend) -> None:
        """AC3: Status filter 'running' returns only running jobs from DB."""
        _save_job(backend, "job-running-1", "running")
        _save_job(backend, "job-pending-1", "pending")
        _save_job(backend, "job-completed-1", "completed")

        jobs, total_count = backend.list_jobs_filtered(status="running")
        assert len(jobs) == 1
        assert total_count == 1
        assert jobs[0]["status"] == "running"

    def test_no_status_filter_returns_all_jobs(self, backend) -> None:
        """No status filter returns all jobs regardless of status."""
        _save_job(backend, "job-1", "completed")
        _save_job(backend, "job-2", "failed")
        _save_job(backend, "job-3", "running")
        _save_job(backend, "job-4", "pending")

        jobs, total_count = backend.list_jobs_filtered()
        assert len(jobs) == 4
        assert total_count == 4

    def test_status_filter_nonexistent_returns_empty(self, backend) -> None:
        """AC12: Status filter that matches nothing returns empty list."""
        _save_job(backend, "job-1", "completed")
        jobs, total_count = backend.list_jobs_filtered(status="cancelled")
        assert jobs == []
        assert total_count == 0


class TestListJobsFilteredByOperationType:
    """Tests for operation_type filter (AC4)."""

    def test_operation_type_filter_returns_only_matching(self, backend) -> None:
        """AC4: Type filter works at database level."""
        _save_job(backend, "job-sync-1", "completed", operation_type="sync_repository")
        _save_job(backend, "job-sync-2", "completed", operation_type="sync_repository")
        _save_job(backend, "job-add-1", "completed", operation_type="add_golden_repo")
        _save_job(backend, "job-scip-1", "completed", operation_type="scip_generate")

        jobs, total_count = backend.list_jobs_filtered(operation_type="sync_repository")
        assert len(jobs) == 2
        assert total_count == 2
        for job in jobs:
            assert job["operation_type"] == "sync_repository"

    def test_operation_type_filter_combined_with_status(self, backend) -> None:
        """AC7: Combined filters work: operation_type AND status."""
        _save_job(backend, "job-1", "completed", operation_type="sync_repository")
        _save_job(backend, "job-2", "failed", operation_type="sync_repository")
        _save_job(backend, "job-3", "completed", operation_type="add_golden_repo")

        jobs, total_count = backend.list_jobs_filtered(
            status="completed", operation_type="sync_repository"
        )
        assert len(jobs) == 1
        assert total_count == 1
        assert jobs[0]["job_id"] == "job-1"

    def test_operation_type_nonexistent_returns_empty(self, backend) -> None:
        """AC12: Non-matching operation_type returns empty."""
        _save_job(backend, "job-1", "completed", operation_type="sync_repository")
        jobs, total_count = backend.list_jobs_filtered(operation_type="nonexistent_op")
        assert jobs == []
        assert total_count == 0


class TestListJobsFilteredByTextSearch:
    """Tests for text search filter (AC5)."""

    def test_search_by_repo_alias_case_insensitive(self, backend) -> None:
        """AC5: Text search on repo_alias (case-insensitive)."""
        _save_job(backend, "job-1", "completed", repo_alias="my-awesome-repo")
        _save_job(backend, "job-2", "completed", repo_alias="other-repo")
        _save_job(backend, "job-3", "completed", repo_alias="My-Awesome-Project")

        jobs, total_count = backend.list_jobs_filtered(search_text="awesome")
        assert total_count == 2, f"Expected 2 matching, got {total_count}"
        job_ids = {j["job_id"] for j in jobs}
        assert "job-1" in job_ids
        assert "job-3" in job_ids
        assert "job-2" not in job_ids

    def test_search_by_username(self, backend) -> None:
        """AC5: Text search on username."""
        _save_job(backend, "job-1", "completed", username="alice")
        _save_job(backend, "job-2", "completed", username="bob")
        _save_job(backend, "job-3", "completed", username="alice-admin")

        jobs, total_count = backend.list_jobs_filtered(search_text="alice")
        assert total_count == 2
        job_ids = {j["job_id"] for j in jobs}
        assert "job-1" in job_ids
        assert "job-3" in job_ids

    def test_search_combined_with_status_filter(self, backend) -> None:
        """AC7: Combined text search and status filter."""
        _save_job(backend, "job-1", "completed", repo_alias="target-repo")
        _save_job(backend, "job-2", "failed", repo_alias="target-repo")
        _save_job(backend, "job-3", "completed", repo_alias="other-repo")

        jobs, total_count = backend.list_jobs_filtered(
            status="completed", search_text="target"
        )
        assert total_count == 1
        assert jobs[0]["job_id"] == "job-1"

    def test_search_no_match_returns_empty(self, backend) -> None:
        """AC12: Text search with no matches returns empty."""
        _save_job(backend, "job-1", "completed", repo_alias="my-repo")
        jobs, total_count = backend.list_jobs_filtered(search_text="zzz-nonexistent")
        assert jobs == []
        assert total_count == 0


class TestListJobsFilteredExcludeIds:
    """Tests for exclude_ids parameter (active jobs dedup)."""

    def test_exclude_ids_omits_specified_jobs(self, backend) -> None:
        """Jobs in exclude_ids must not appear in results even if they match filters."""
        _save_job(backend, "job-active-1", "running")
        _save_job(backend, "job-active-2", "pending")
        _save_job(backend, "job-historical-1", "completed")
        _save_job(backend, "job-historical-2", "failed")

        # Exclude the active jobs (as if they came from memory)
        jobs, total_count = backend.list_jobs_filtered(
            exclude_ids={"job-active-1", "job-active-2"}
        )
        assert total_count == 2, f"Expected 2 non-excluded jobs, got {total_count}"
        job_ids = {j["job_id"] for j in jobs}
        assert "job-active-1" not in job_ids
        assert "job-active-2" not in job_ids
        assert "job-historical-1" in job_ids
        assert "job-historical-2" in job_ids

    def test_exclude_ids_empty_set_does_not_filter(self, backend) -> None:
        """Empty exclude_ids set returns all matching jobs."""
        _save_job(backend, "job-1", "completed")
        _save_job(backend, "job-2", "failed")

        jobs, total_count = backend.list_jobs_filtered(exclude_ids=set())
        assert total_count == 2

    def test_exclude_all_returns_empty(self, backend) -> None:
        """Excluding all jobs returns empty."""
        _save_job(backend, "job-1", "completed")
        _save_job(backend, "job-2", "failed")

        jobs, total_count = backend.list_jobs_filtered(
            exclude_ids={"job-1", "job-2"}
        )
        assert jobs == []
        assert total_count == 0


class TestListJobsFilteredPagination:
    """Tests for pagination (AC6)."""

    def test_pagination_limit_restricts_results(self, backend) -> None:
        """AC6: limit parameter restricts number of returned jobs."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-{i:02d}", "completed", created_at=ts)

        jobs, total_count = backend.list_jobs_filtered(limit=3)
        assert len(jobs) == 3, f"Expected 3 results, got {len(jobs)}"
        assert total_count == 10, f"Total count must be 10, got {total_count}"

    def test_pagination_offset_skips_results(self, backend) -> None:
        """AC6: offset parameter skips records for page navigation."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-{i:02d}", "completed", created_at=ts)

        # Page 1: first 5
        jobs_p1, total_count = backend.list_jobs_filtered(limit=5, offset=0)
        # Page 2: next 5
        jobs_p2, total_count2 = backend.list_jobs_filtered(limit=5, offset=5)

        assert len(jobs_p1) == 5
        assert len(jobs_p2) == 5
        assert total_count == 10
        assert total_count2 == 10

        # No overlap between pages
        ids_p1 = {j["job_id"] for j in jobs_p1}
        ids_p2 = {j["job_id"] for j in jobs_p2}
        assert ids_p1.isdisjoint(ids_p2), "Pages must not overlap"

    def test_pagination_total_count_is_unaffected_by_limit(self, backend) -> None:
        """AC6: total_count reflects the full matching set, not the page size."""
        now = datetime.now(timezone.utc)
        for i in range(20):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-{i:02d}", "completed", created_at=ts)

        jobs, total_count = backend.list_jobs_filtered(limit=5, offset=0)
        assert len(jobs) == 5
        assert total_count == 20, (
            f"total_count must reflect all 20 matching jobs, got {total_count}"
        )

    def test_pagination_last_page_has_fewer_results(self, backend) -> None:
        """AC6: Last page may have fewer results than page_size."""
        now = datetime.now(timezone.utc)
        for i in range(7):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-{i:02d}", "completed", created_at=ts)

        jobs_p1, total = backend.list_jobs_filtered(limit=5, offset=0)
        jobs_p2, _ = backend.list_jobs_filtered(limit=5, offset=5)

        assert len(jobs_p1) == 5
        assert len(jobs_p2) == 2
        assert total == 7

    def test_pagination_with_status_filter(self, backend) -> None:
        """AC7: Pagination works correctly combined with status filter."""
        now = datetime.now(timezone.utc)
        for i in range(8):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-completed-{i:02d}", "completed", created_at=ts)
        for i in range(5):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(backend, f"job-failed-{i:02d}", "failed", created_at=ts)

        # With status=completed, total should be 8
        jobs, total_count = backend.list_jobs_filtered(
            status="completed", limit=3, offset=0
        )
        assert total_count == 8, f"Expected 8 completed jobs total, got {total_count}"
        assert len(jobs) == 3

    def test_pagination_offset_beyond_total_returns_empty(self, backend) -> None:
        """Offset beyond total count returns empty list but correct total_count."""
        for i in range(3):
            _save_job(backend, f"job-{i}", "completed")

        jobs, total_count = backend.list_jobs_filtered(limit=5, offset=100)
        assert jobs == []
        assert total_count == 3, "total_count must still reflect all matching jobs"


class TestListJobsFilteredOrdering:
    """Tests for ordering of results (most recent first)."""

    def test_results_ordered_by_created_at_desc(self, backend) -> None:
        """Results are ordered by created_at descending (newest first)."""
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        _save_job(backend, "job-old", "completed",
                  created_at=(base).isoformat())
        _save_job(backend, "job-mid", "completed",
                  created_at=(base + timedelta(hours=1)).isoformat())
        _save_job(backend, "job-new", "completed",
                  created_at=(base + timedelta(hours=2)).isoformat())

        jobs, _ = backend.list_jobs_filtered()
        assert jobs[0]["job_id"] == "job-new"
        assert jobs[1]["job_id"] == "job-mid"
        assert jobs[2]["job_id"] == "job-old"


class TestListJobsFilteredAllCombined:
    """Tests that combine all filter parameters simultaneously (AC7)."""

    def test_all_filters_combined(self, backend) -> None:
        """AC7: All filters (status + type + search + exclude + pagination) combined."""
        now = datetime.now(timezone.utc)
        # 10 completed sync_repository jobs for "my-repo" by alice
        for i in range(10):
            ts = (now - timedelta(minutes=i)).isoformat()
            _save_job(
                backend,
                f"job-target-{i:02d}",
                "completed",
                operation_type="sync_repository",
                username="alice",
                repo_alias="my-repo",
                created_at=ts,
            )
        # Some non-matching jobs
        _save_job(
            backend, "job-other-type", "completed",
            operation_type="add_golden_repo", username="alice", repo_alias="my-repo",
        )
        _save_job(
            backend, "job-other-user", "completed",
            operation_type="sync_repository", username="bob", repo_alias="my-repo",
        )
        _save_job(
            backend, "job-other-status", "failed",
            operation_type="sync_repository", username="alice", repo_alias="my-repo",
        )

        # Exclude 2 jobs that are "in memory"
        exclude = {"job-target-00", "job-target-01"}

        jobs, total_count = backend.list_jobs_filtered(
            status="completed",
            operation_type="sync_repository",
            search_text="my-repo",
            exclude_ids=exclude,
            limit=5,
            offset=0,
        )

        # 10 target jobs - 2 excluded = 8 target remaining, + 1 job-other-user = 9 matching;
        # job-other-user has repo_alias="my-repo" so it correctly matches search_text="my-repo"
        assert total_count == 9, f"Expected 9 after exclusion, got {total_count}"
        assert len(jobs) == 5, f"Expected 5 on page 1, got {len(jobs)}"
        returned_ids = {j["job_id"] for j in jobs}
        assert "job-target-00" not in returned_ids
        assert "job-target-01" not in returned_ids
