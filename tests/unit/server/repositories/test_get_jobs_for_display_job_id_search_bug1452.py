"""
Unit tests for Bug #1452 (3rd follow-up): BackgroundJobManager.get_jobs_for_display()
in-memory active-jobs search predicate must also match job_id.

Recap: the two prior #1452 fixes corrected the `status`/`status_filter` and
`search_text`/`search` query-param name mismatches between the deactivation
success-message "Job ID: <link>" and the /admin/jobs route. Those fixes are
now correct -- the param genuinely reaches the backend search filter. BUT a
real end-to-end test proved that clicking the link (which points to
`/admin/jobs?search=<job_id>`) still renders "No jobs found", because the
search predicate itself has never matched the job_id column/attribute at
all -- only repo_alias, username, operation_type, and error.

This file covers the in-memory (active RUNNING/PENDING job) branch of
`get_jobs_for_display`, at src/code_indexer/server/repositories/background_jobs.py
around line 2894-2906. The DB-backed branch (SQLite/PostgreSQL
`list_jobs_filtered`) is covered separately in
tests/unit/server/storage/test_jobs_filtered_query.py and
tests/unit/server/storage/postgres/test_background_jobs_postgres.py.
"""

import uuid
from datetime import datetime, timezone

from src.code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from src.code_indexer.server.utils.config_manager import BackgroundJobsConfig

_MAX_CONCURRENT_JOBS = 10


def _make_manager() -> BackgroundJobManager:
    """Create an in-memory (non-SQLite) BackgroundJobManager."""
    return BackgroundJobManager(
        use_sqlite=False,
        background_jobs_config=BackgroundJobsConfig(
            max_concurrent_background_jobs=_MAX_CONCURRENT_JOBS,
        ),
    )


def _inject_job(
    manager: BackgroundJobManager,
    job_id: str,
    username: str = "admin_user",
    operation_type: str = "deactivate_repository",
    status: JobStatus = JobStatus.PENDING,
) -> None:
    """Inject a job directly into the manager's in-memory dict."""
    job = BackgroundJob(
        job_id=job_id,
        operation_type=operation_type,
        status=status,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        result=None,
        error=None,
        progress=0,
        username=username,
    )
    with manager._lock:
        manager.jobs[job_id] = job


class TestGetJobsForDisplayJobIdSearch:
    """Searching by job_id must find an active (RUNNING/PENDING) in-memory job."""

    def setup_method(self):
        self.manager = _make_manager()

    def teardown_method(self):
        self.manager.shutdown()

    def test_search_by_exact_job_id_finds_active_job(self):
        """Reproduces the real-world symptom: clicking the deactivation
        success message's 'Job ID: <link>' (?search=<job_id>) must find the
        job, not render 'No jobs found'.
        """
        target_job_id = "9740fda1-102e-4213-875b-c6124e1b62b2"
        _inject_job(self.manager, target_job_id, status=JobStatus.PENDING)
        _inject_job(
            self.manager, str(uuid.uuid4()), status=JobStatus.RUNNING
        )  # unrelated job, must not match

        jobs, total, _pages = self.manager.get_jobs_for_display(
            search_text=target_job_id
        )
        job_ids = [j["job_id"] for j in jobs]

        assert target_job_id in job_ids, (
            f"Expected job_id search to find {target_job_id}, got jobs: {job_ids}"
        )
        assert total == 1, f"Expected exactly 1 matching job, got total={total}"

    def test_search_by_partial_job_id_substring_finds_active_job(self):
        """A partial/substring job_id search must also match, consistent with
        the existing substring-match behavior for repo_alias/username/etc.
        """
        target_job_id = "9740fda1-102e-4213-875b-c6124e1b62b2"
        _inject_job(self.manager, target_job_id, status=JobStatus.RUNNING)

        jobs, total, _pages = self.manager.get_jobs_for_display(search_text="102e-4213")
        job_ids = [j["job_id"] for j in jobs]

        assert target_job_id in job_ids
        assert total == 1

    def test_existing_repo_alias_search_still_works(self):
        """Regression guard: adding job_id matching must not break the
        pre-existing repo_alias/username/operation_type/error search.
        """
        job_id = str(uuid.uuid4())
        job = BackgroundJob(
            job_id=job_id,
            operation_type="sync_repository",
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="admin_user",
            repo_alias="my-awesome-repo",
        )
        with self.manager._lock:
            self.manager.jobs[job_id] = job

        jobs, total, _pages = self.manager.get_jobs_for_display(search_text="awesome")
        job_ids = [j["job_id"] for j in jobs]

        assert job_id in job_ids
        assert total == 1
