"""Bug #67: Verify xray_search and xray_explore jobs appear in dashboard recent-jobs widget.

Investigation finding: Neither BackgroundJobManager.get_recent_jobs_with_filter nor
JobTracker.get_recent_jobs filter by operation_type. The dashboard template
dashboard_recent_jobs.html has no operation_type exclusion list.
xray_search and xray_explore jobs submitted via submit_job() appear automatically.

These tests prove that behaviour is preserved and cannot regress.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from typing import List

import pytest

from code_indexer.server.services.dashboard_service import DashboardService


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_job_dict(
    job_id: str,
    operation_type: str,
    status: str = "completed",
    repo_alias: str = "my-repo",
) -> dict:
    """Build a minimal job dict as returned by get_recent_jobs_with_filter."""
    now = datetime.now(timezone.utc)
    completed_at = (now - timedelta(minutes=5)).isoformat()
    created_at = (now - timedelta(minutes=10)).isoformat()
    return {
        "job_id": job_id,
        "operation_type": operation_type,
        "status": status,
        "created_at": created_at,
        "started_at": created_at,
        "completed_at": completed_at,
        "progress": 100,
        "result": {},
        "error": None,
        "username": "testuser",
        "repo_alias": repo_alias,
    }


def _query_recent_jobs(
    dashboard_service: DashboardService,
    jobs: List[dict],
) -> tuple:
    """
    Call dashboard_service._get_recent_jobs with a mock BackgroundJobManager
    that returns the given jobs list.  JobTracker is disabled (None) so the
    fallback path through get_recent_jobs_with_filter is exercised.

    Returns (recent_jobs_list, mock_job_manager) so callers can assert on
    both the result and the mock's call history.
    """
    mock_mgr = MagicMock()
    mock_mgr.get_recent_jobs_with_filter.return_value = jobs
    mock_mgr.get_job_stats_with_filter.return_value = {"completed": 1, "failed": 0}
    mock_mgr.get_active_job_count.return_value = 0
    mock_mgr.get_pending_job_count.return_value = 0

    with patch.object(
        dashboard_service, "_get_background_job_manager", return_value=mock_mgr
    ):
        with patch.object(dashboard_service, "_get_job_tracker", return_value=None):
            recent = dashboard_service._get_recent_jobs("testuser", "30d")

    return recent, mock_mgr


@pytest.fixture()
def dashboard_service() -> DashboardService:
    return DashboardService()


# ---------------------------------------------------------------------------
# AC1: xray_search jobs appear in recent-jobs query result
# ---------------------------------------------------------------------------


class TestXraySearchJobsAppearInRecentJobs:
    """xray_search operation_type must be returned by _get_recent_jobs."""

    def test_xray_search_job_appears_in_recent_jobs_query(self, dashboard_service):
        """
        _get_recent_jobs returns xray_search jobs.

        Given a BackgroundJobManager that holds one completed xray_search job
        When _get_recent_jobs is called via DashboardService
        Then the xray_search job appears in the result and
        get_recent_jobs_with_filter was the query method invoked.
        """
        jobs = [_make_job_dict("xray-001", "xray_search")]
        recent, mock_mgr = _query_recent_jobs(dashboard_service, jobs)

        job_types = [j.job_type for j in recent]
        assert "xray_search" in job_types, (
            f"xray_search not found in recent jobs. Got: {job_types}"
        )
        mock_mgr.get_recent_jobs_with_filter.assert_called_once()


# ---------------------------------------------------------------------------
# AC2: xray_explore jobs appear in recent-jobs query result
# ---------------------------------------------------------------------------


class TestXrayExploreJobsAppearInRecentJobs:
    """xray_explore operation_type must be returned by _get_recent_jobs."""

    def test_xray_explore_job_appears_in_recent_jobs_query(self, dashboard_service):
        """
        _get_recent_jobs returns xray_explore jobs.

        Given a BackgroundJobManager that holds one completed xray_explore job
        When _get_recent_jobs is called via DashboardService
        Then the xray_explore job appears in the result and
        get_recent_jobs_with_filter was the query method invoked.
        """
        jobs = [_make_job_dict("xray-explore-001", "xray_explore")]
        recent, mock_mgr = _query_recent_jobs(dashboard_service, jobs)

        job_types = [j.job_type for j in recent]
        assert "xray_explore" in job_types, (
            f"xray_explore not found in recent jobs. Got: {job_types}"
        )
        mock_mgr.get_recent_jobs_with_filter.assert_called_once()


# ---------------------------------------------------------------------------
# AC3: Regression — other operation types still appear
# ---------------------------------------------------------------------------


class TestOtherOperationTypesStillAppear:
    """search_code and regex_search jobs must not be accidentally excluded."""

    def test_mixed_xray_and_other_jobs_all_appear(self, dashboard_service):
        """
        When xray_search, xray_explore, search_code, and regex_search jobs coexist,
        all four operation types appear in the recent-jobs result.

        This is the primary regression guard: any inadvertent operation_type filter
        would cause one or more types to disappear from this list.
        get_recent_jobs_with_filter must be the query path used (not JobTracker).
        """
        jobs = [
            _make_job_dict("j1", "xray_search"),
            _make_job_dict("j2", "xray_explore"),
            _make_job_dict("j3", "search_code"),
            _make_job_dict("j4", "regex_search"),
        ]
        recent, mock_mgr = _query_recent_jobs(dashboard_service, jobs)

        job_types = {j.job_type for j in recent}
        assert "xray_search" in job_types
        assert "xray_explore" in job_types
        assert "search_code" in job_types
        assert "regex_search" in job_types
        mock_mgr.get_recent_jobs_with_filter.assert_called_once()
