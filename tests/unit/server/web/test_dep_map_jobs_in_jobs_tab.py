"""
Regression tests for Bug #736: Dependency map refresh jobs missing from Jobs tab.

Dependency map jobs register exclusively with JobTracker.  The Jobs tab
(_get_all_jobs in web/routes.py) previously only consulted BackgroundJobManager,
so dependency map jobs were invisible on the /jobs page while the dashboard
showed them correctly via JobTracker.

Fix: _get_all_jobs() must merge results from both tracking systems,
deduplicating by job_id.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from code_indexer.server.web.routes import _get_all_jobs

# Named constants — no magic numbers in test bodies
DEP_MAP_FULL_TYPE = "dependency_map_full"
DEP_MAP_DELTA_TYPE = "dependency_map_delta"
SYNC_TYPE = "sync_repository"
JOB_ID_DEP_MAP = "dep-map-job-001"
JOB_ID_SYNC = "sync-job-001"
JOB_ID_SHARED = "shared-job-001"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"


def _make_job_dict(
    job_id: str, operation_type: str, status: str = STATUS_COMPLETED
) -> dict:
    """Return a minimal job dict in the shape both tracking systems return."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "job_id": job_id,
        "operation_type": operation_type,
        "status": status,
        "username": "admin",
        "repo_alias": None,
        "progress": 100,
        "progress_info": None,
        "metadata": None,
        "created_at": now,
        "started_at": now,
        "completed_at": now,
        "error": None,
        "result": None,
    }


def _run_get_all_jobs(bg_jobs: list, tracker_jobs: list):
    """Run _get_all_jobs() with mocked tracking systems and return (jobs, total, pages)."""
    bg_manager = MagicMock()
    bg_manager.get_jobs_for_display.return_value = (bg_jobs, len(bg_jobs), 1)

    job_tracker = MagicMock()
    job_tracker.get_recent_jobs.return_value = tracker_jobs

    with patch(
        "code_indexer.server.web.routes._get_background_job_manager",
        return_value=bg_manager,
    ):
        with patch(
            "code_indexer.server.web.routes._get_job_tracker",
            return_value=job_tracker,
        ):
            return _get_all_jobs()


class TestGetAllJobsJobTrackerVisibility:
    """Jobs registered only in JobTracker must be visible on the Jobs tab."""

    def test_dep_map_full_job_tracker_only_appears(self):
        """A dependency_map_full job registered only in JobTracker must appear.

        Core regression: previously _get_all_jobs() ignored JobTracker entirely.
        """
        dep_map_job = _make_job_dict(JOB_ID_DEP_MAP, DEP_MAP_FULL_TYPE, STATUS_RUNNING)

        jobs, total_count, _pages = _run_get_all_jobs(
            bg_jobs=[],
            tracker_jobs=[dep_map_job],
        )

        job_ids = [j["job_id"] for j in jobs]
        assert JOB_ID_DEP_MAP in job_ids, (
            f"Bug #736: {DEP_MAP_FULL_TYPE!r} job not found in Jobs tab. Found: {job_ids}"
        )
        assert total_count >= 1

    def test_dep_map_delta_job_type_appears(self):
        """dependency_map_delta jobs must also be visible (the more common type)."""
        delta_job = _make_job_dict(JOB_ID_DEP_MAP, DEP_MAP_DELTA_TYPE, STATUS_RUNNING)

        jobs, _total, _pages = _run_get_all_jobs(
            bg_jobs=[],
            tracker_jobs=[delta_job],
        )

        op_types = [j["operation_type"] for j in jobs]
        assert DEP_MAP_DELTA_TYPE in op_types, (
            f"Bug #736: {DEP_MAP_DELTA_TYPE!r} job not found. Found types: {op_types}"
        )

    def test_background_manager_jobs_still_appear(self):
        """BackgroundJobManager results must not be dropped by the merge.

        Regression guard: the fix must preserve all pre-existing job visibility.
        """
        sync_job = _make_job_dict(JOB_ID_SYNC, SYNC_TYPE, STATUS_COMPLETED)

        jobs, _total, _pages = _run_get_all_jobs(
            bg_jobs=[sync_job],
            tracker_jobs=[],
        )

        job_ids = [j["job_id"] for j in jobs]
        assert JOB_ID_SYNC in job_ids, (
            f"BackgroundJobManager job {JOB_ID_SYNC!r} disappeared after merge. "
            f"Found: {job_ids}"
        )


class TestGetAllJobsDeduplication:
    """Jobs present in both tracking systems must appear exactly once."""

    def test_duplicate_job_appears_exactly_once(self):
        """A job known to both systems must not be listed twice."""
        shared_bg = _make_job_dict(JOB_ID_SHARED, SYNC_TYPE, STATUS_RUNNING)
        shared_tracker = _make_job_dict(JOB_ID_SHARED, SYNC_TYPE, STATUS_RUNNING)

        jobs, _total, _pages = _run_get_all_jobs(
            bg_jobs=[shared_bg],
            tracker_jobs=[shared_tracker],
        )

        matching = [j for j in jobs if j["job_id"] == JOB_ID_SHARED]
        assert len(matching) == 1, (
            f"Job {JOB_ID_SHARED!r} appears {len(matching)} times; "
            "deduplication required when job exists in both tracking systems"
        )
