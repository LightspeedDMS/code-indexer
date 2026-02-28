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

    def test_get_recent_jobs_sorted_by_priority_then_time(self, tracker):
        """
        get_recent_jobs returns running jobs first, then pending, then completed.

        Story #328: Running jobs appear above completed jobs regardless of timestamps.

        Given one running job (registered first) and one completed job (registered after)
        When get_recent_jobs is called
        Then the running job appears before the completed job
        """
        tracker.register_job("job-sort-running-001", "op_a", "admin")
        tracker.update_status("job-sort-running-001", status="running")
        time.sleep(0.01)
        tracker.register_job("job-sort-completed-002", "op_b", "admin")
        tracker.update_status("job-sort-completed-002", status="running")
        tracker.complete_job("job-sort-completed-002")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        ids = [
            j["job_id"]
            for j in jobs
            if j["job_id"].startswith("job-sort-")
        ]
        assert ids[0] == "job-sort-running-001"
        assert ids[1] == "job-sort-completed-002"

    # ------------------------------------------------------------------
    # Story #328: Dashboard Recent Activity - Running Jobs on Top
    # Acceptance Criteria tests
    # ------------------------------------------------------------------

    def test_ac1_running_jobs_appear_above_completed_jobs(self, tracker):
        """
        AC1: Running jobs appear above completed jobs.

        Given the dashboard has 2 running jobs and 3 completed jobs
        When I view the Recent Activity section
        Then the 2 running jobs appear in rows 1-2
        And the 3 completed jobs appear in rows 3-5
        """
        # Register and complete 3 jobs first
        for i in range(1, 4):
            jid = f"job-ac1-completed-{i:03d}"
            tracker.register_job(jid, "op_c", "admin")
            tracker.update_status(jid, status="running")
            tracker.complete_job(jid)
            time.sleep(0.01)

        # Register 2 running jobs
        for i in range(1, 3):
            jid = f"job-ac1-running-{i:03d}"
            tracker.register_job(jid, "op_r", "admin")
            tracker.update_status(jid, status="running")
            time.sleep(0.01)

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        relevant = [j for j in jobs if j["job_id"].startswith("job-ac1-")]

        assert len(relevant) == 5
        # First 2 should be running
        assert relevant[0]["status"] == "running"
        assert relevant[1]["status"] == "running"
        # Last 3 should be completed
        assert relevant[2]["status"] == "completed"
        assert relevant[3]["status"] == "completed"
        assert relevant[4]["status"] == "completed"

    def test_ac2_running_jobs_sorted_by_most_recently_scheduled_first(self, tracker):
        """
        AC2: Running jobs sorted by most recently scheduled (started_at) first.

        Given there are 3 running jobs scheduled at 10:00, 11:00, and 12:00
        When I view the Recent Activity section
        Then the 12:00 job appears first, then 11:00, then 10:00
        """
        tracker.register_job("job-ac2-run-001", "op_r", "admin")
        tracker.update_status("job-ac2-run-001", status="running")
        time.sleep(0.01)
        tracker.register_job("job-ac2-run-002", "op_r", "admin")
        tracker.update_status("job-ac2-run-002", status="running")
        time.sleep(0.01)
        tracker.register_job("job-ac2-run-003", "op_r", "admin")
        tracker.update_status("job-ac2-run-003", status="running")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        ids = [j["job_id"] for j in jobs if j["job_id"].startswith("job-ac2-run-")]

        assert len(ids) == 3
        # Most recently started appears first
        assert ids[0] == "job-ac2-run-003"
        assert ids[1] == "job-ac2-run-002"
        assert ids[2] == "job-ac2-run-001"

    def test_ac3_completed_jobs_sorted_by_most_recently_completed_first(self, tracker):
        """
        AC3: Completed jobs sorted by most recently completed first.

        Given there are 3 completed jobs finished at 09:00, 10:00, and 11:00
        When I view the Recent Activity section (below any running jobs)
        Then the 11:00 completed job appears first, then 10:00, then 09:00
        """
        tracker.register_job("job-ac3-comp-001", "op_c", "admin")
        tracker.update_status("job-ac3-comp-001", status="running")
        tracker.complete_job("job-ac3-comp-001")
        time.sleep(0.01)
        tracker.register_job("job-ac3-comp-002", "op_c", "admin")
        tracker.update_status("job-ac3-comp-002", status="running")
        tracker.complete_job("job-ac3-comp-002")
        time.sleep(0.01)
        tracker.register_job("job-ac3-comp-003", "op_c", "admin")
        tracker.update_status("job-ac3-comp-003", status="running")
        tracker.complete_job("job-ac3-comp-003")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        ids = [j["job_id"] for j in jobs if j["job_id"].startswith("job-ac3-comp-")]

        assert len(ids) == 3
        # Most recently completed appears first
        assert ids[0] == "job-ac3-comp-003"
        assert ids[1] == "job-ac3-comp-002"
        assert ids[2] == "job-ac3-comp-001"

    def test_ac4_pending_jobs_grouped_above_completed(self, tracker):
        """
        AC4: Pending jobs grouped with running (above completed).

        Given there is 1 pending job and 2 completed jobs
        When I view the Recent Activity section
        Then the pending job appears in row 1
        And the completed jobs appear in rows 2-3
        """
        # Register 2 completed jobs first
        for i in range(1, 3):
            jid = f"job-ac4-completed-{i:03d}"
            tracker.register_job(jid, "op_c", "admin")
            tracker.update_status(jid, status="running")
            tracker.complete_job(jid)
            time.sleep(0.01)

        # Register 1 pending job (stays in pending status)
        tracker.register_job("job-ac4-pending-001", "op_p", "admin")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        relevant = [j for j in jobs if j["job_id"].startswith("job-ac4-")]

        assert len(relevant) == 3
        # First row is pending
        assert relevant[0]["job_id"] == "job-ac4-pending-001"
        assert relevant[0]["status"] == "pending"
        # Rows 2-3 are completed
        assert relevant[1]["status"] == "completed"
        assert relevant[2]["status"] == "completed"

    def test_ac5_running_job_with_older_timestamp_beats_completed_with_newer(
        self, tracker
    ):
        """
        AC5: Running job with older timestamp appears above completed job with newer timestamp.

        Given there is 1 running job created at 08:00
        And there is 1 completed job that finished at 12:00
        When I view the Recent Activity section
        Then the running job (08:00) appears in row 1
        And the completed job (12:00) appears in row 2
        """
        # Register and complete a job (newer timestamps overall)
        tracker.register_job("job-ac5-completed-001", "op_c", "admin")
        tracker.update_status("job-ac5-completed-001", status="running")
        tracker.complete_job("job-ac5-completed-001")
        time.sleep(0.01)

        # Register a running job (older created_at, but still running)
        # This job was registered AFTER the completed one, but status = running beats completed
        tracker.register_job("job-ac5-running-001", "op_r", "admin")
        tracker.update_status("job-ac5-running-001", status="running")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        relevant = [j for j in jobs if j["job_id"].startswith("job-ac5-")]

        assert len(relevant) == 2
        # Running job appears first regardless of timestamps
        assert relevant[0]["job_id"] == "job-ac5-running-001"
        assert relevant[0]["status"] == "running"
        assert relevant[1]["job_id"] == "job-ac5-completed-001"
        assert relevant[1]["status"] == "completed"

    def test_resolving_prerequisites_grouped_with_pending_above_completed(
        self, tracker
    ):
        """
        Story #328: resolving_prerequisites status treated same as pending (priority 1).

        Given a job with status resolving_prerequisites and a completed job
        When get_recent_jobs is called
        Then the resolving_prerequisites job appears above the completed job
        """
        tracker.register_job("job-rp-completed-001", "op_c", "admin")
        tracker.update_status("job-rp-completed-001", status="running")
        tracker.complete_job("job-rp-completed-001")
        time.sleep(0.01)

        tracker.register_job("job-rp-resolving-001", "op_r", "admin")
        tracker.update_status("job-rp-resolving-001", status="resolving_prerequisites")

        jobs = tracker.get_recent_jobs(limit=10, time_filter="all")
        relevant = [j for j in jobs if j["job_id"].startswith("job-rp-")]

        assert len(relevant) == 2
        assert relevant[0]["job_id"] == "job-rp-resolving-001"
        assert relevant[0]["status"] == "resolving_prerequisites"
        assert relevant[1]["job_id"] == "job-rp-completed-001"
        assert relevant[1]["status"] == "completed"


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
