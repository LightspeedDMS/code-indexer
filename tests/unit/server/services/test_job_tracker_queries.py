"""
Unit tests for JobTracker query methods: get_job, get_active_jobs,
get_recent_jobs, and query_jobs.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
Covers AC3: get_job, get_active_jobs, get_recent_jobs, query_jobs
"""

import time

import pytest



class TestJobTrackerGetJob:
    """Tests for get_job (AC3)."""

    def test_get_job_from_memory(self, tracker):
        """
        get_job returns the active job from the in-memory dict.

        Given a registered (active) job
        When get_job is called
        Then the TrackedJob is returned from memory
        """
        tracker.register_job("job-getMem-001", "dep_map_analysis", "admin")
        job = tracker.get_job("job-getMem-001")

        assert job is not None
        assert job.job_id == "job-getMem-001"

    def test_get_job_from_sqlite(self, tracker):
        """
        get_job falls back to SQLite for completed jobs no longer in memory.

        Given a job that has been completed (removed from memory)
        When get_job is called
        Then the job is fetched from SQLite
        """
        tracker.register_job("job-getSql-001", "dep_map_analysis", "admin")
        tracker.update_status("job-getSql-001", status="running")
        tracker.complete_job("job-getSql-001")

        active_ids = [j.job_id for j in tracker.get_active_jobs()]
        assert "job-getSql-001" not in active_ids

        job = tracker.get_job("job-getSql-001")
        assert job is not None
        assert job.job_id == "job-getSql-001"
        assert job.status == "completed"

    def test_get_job_returns_none_for_missing(self, tracker):
        """
        get_job returns None for a job_id that was never registered.

        Given a job_id that does not exist
        When get_job is called
        Then None is returned
        """
        result = tracker.get_job("totally-unknown-id")
        assert result is None


class TestJobTrackerGetActiveJobs:
    """Tests for get_active_jobs (AC3)."""

    def test_get_active_jobs_returns_all_active(self, tracker):
        """
        get_active_jobs returns all in-memory active/pending jobs.

        Given three registered jobs
        When get_active_jobs is called
        Then all three are returned
        """
        tracker.register_job("job-active-001", "op_a", "admin")
        tracker.register_job("job-active-002", "op_b", "admin")
        tracker.register_job("job-active-003", "op_c", "admin")

        active = tracker.get_active_jobs()
        ids = {j.job_id for j in active}
        assert "job-active-001" in ids
        assert "job-active-002" in ids
        assert "job-active-003" in ids

    def test_get_active_jobs_excludes_completed(self, tracker):
        """
        get_active_jobs excludes jobs that have been completed.

        Given a completed job and an active job
        When get_active_jobs is called
        Then only the active job is returned
        """
        tracker.register_job("job-excl-001", "op_a", "admin")
        tracker.register_job("job-excl-002", "op_b", "admin")
        tracker.complete_job("job-excl-001")

        active = tracker.get_active_jobs()
        ids = {j.job_id for j in active}
        assert "job-excl-001" not in ids
        assert "job-excl-002" in ids


class TestJobTrackerGetRecentJobs:
    """Tests for get_recent_jobs (AC3)."""

    def test_get_recent_jobs_includes_active_and_historical(self, tracker):
        """
        get_recent_jobs merges active in-memory jobs with SQLite historical jobs.

        Given one active job and one completed job
        When get_recent_jobs is called
        Then both jobs appear in the results
        """
        tracker.register_job("job-recent-001", "op_a", "admin")
        tracker.register_job("job-recent-002", "op_b", "admin")
        tracker.complete_job("job-recent-002")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        ids = {j["job_id"] for j in jobs}
        assert "job-recent-001" in ids
        assert "job-recent-002" in ids

    def test_get_recent_jobs_deduplication(self, tracker):
        """
        get_recent_jobs does not return the same job twice.

        Given an active job (present in both memory and SQLite)
        When get_recent_jobs is called
        Then the job appears only once in the results
        """
        tracker.register_job("job-dedup-001", "op_a", "admin")

        jobs = tracker.get_recent_jobs(limit=50, time_filter="all")
        ids = [j["job_id"] for j in jobs if j["job_id"] == "job-dedup-001"]
        assert len(ids) == 1

    def test_get_recent_jobs_sorted_by_created_at(self, tracker):
        """
        get_recent_jobs returns jobs sorted most-recently-created first.

        Given three jobs registered in order (oldest to newest)
        When get_recent_jobs is called
        Then the newest job appears first in the list
        """
        tracker.register_job("job-sort-001", "op_a", "admin")
        time.sleep(0.01)
        tracker.register_job("job-sort-002", "op_b", "admin")
        time.sleep(0.01)
        tracker.register_job("job-sort-003", "op_c", "admin")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        ids = [j["job_id"] for j in jobs if j["job_id"].startswith("job-sort-")]
        assert ids[0] == "job-sort-003"
        assert ids[-1] == "job-sort-001"


class TestJobTrackerQueryJobs:
    """Tests for query_jobs (AC3)."""

    def test_query_jobs_filter_by_operation_type(self, tracker):
        """
        query_jobs returns only jobs matching the given operation_type.

        Given jobs of different operation types
        When query_jobs(operation_type='dep_map_analysis') is called
        Then only dep_map_analysis jobs are returned
        """
        tracker.register_job("job-qOp-001", "dep_map_analysis", "admin")
        tracker.register_job("job-qOp-002", "description_refresh", "admin")

        results = tracker.query_jobs(operation_type="dep_map_analysis")
        ids = {j["job_id"] for j in results}
        assert "job-qOp-001" in ids
        assert "job-qOp-002" not in ids

    def test_query_jobs_filter_by_status(self, tracker):
        """
        query_jobs returns only jobs with the given status.

        Given pending and running jobs
        When query_jobs(status='running') is called
        Then only running jobs are returned
        """
        tracker.register_job("job-qStat-001", "op_a", "admin")
        tracker.register_job("job-qStat-002", "op_b", "admin")
        tracker.update_status("job-qStat-001", status="running")

        results = tracker.query_jobs(status="running")
        ids = {j["job_id"] for j in results}
        assert "job-qStat-001" in ids
        assert "job-qStat-002" not in ids

    def test_query_jobs_filter_by_repo_alias(self, tracker):
        """
        query_jobs returns only jobs matching the given repo_alias.

        Given jobs with different repo aliases
        When query_jobs(repo_alias='repo-a') is called
        Then only jobs for 'repo-a' are returned
        """
        tracker.register_job("job-qRepo-001", "op_a", "admin", repo_alias="repo-a")
        tracker.register_job("job-qRepo-002", "op_b", "admin", repo_alias="repo-b")

        results = tracker.query_jobs(repo_alias="repo-a")
        ids = {j["job_id"] for j in results}
        assert "job-qRepo-001" in ids
        assert "job-qRepo-002" not in ids

    def test_query_jobs_no_filter(self, tracker):
        """
        query_jobs with no filters returns all jobs.

        Given multiple jobs of different types and statuses
        When query_jobs() is called with no filters
        Then all registered jobs are returned
        """
        tracker.register_job("job-qAll-001", "op_a", "admin")
        tracker.register_job("job-qAll-002", "op_b", "admin")
        tracker.register_job("job-qAll-003", "op_c", "admin")

        results = tracker.query_jobs()
        ids = {j["job_id"] for j in results}
        assert "job-qAll-001" in ids
        assert "job-qAll-002" in ids
        assert "job-qAll-003" in ids
