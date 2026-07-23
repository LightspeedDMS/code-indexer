"""
Tests for Story #1400 Phase 9 (MEDIUM): "temporal_query" jobs must be
hidden from the dashboard on BOTH the JobTracker path (mirrors the existing
xray_search/xray_explore/xray_search_batch entries) AND the no-JobTracker
fallback path -- which, per the locked design, had no exclusion mechanism
at all before this story (see test_background_jobs_dashboard_exclusion_1400.py
for the BackgroundJobManager-level fix this wiring depends on).

Test seam note: patch.object(dashboard_service, "_get_background_job_manager"
/"_get_job_tracker", ...) is this codebase's established DashboardService
test convention for swapping the JobTracker/BackgroundJobManager external
collaborators -- see test_dashboard_service_tracker.py's
TestDashboardRecentJobsWithTracker class (test_get_recent_jobs_uses_tracker,
test_get_recent_jobs_falls_back_when_no_tracker) for precedent. The behavior
under test is the exclude_operation_types list _get_recent_jobs builds and
passes to each collaborator, not the collaborators themselves.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.dashboard_service import DashboardService


@pytest.fixture
def dashboard_service():
    return DashboardService()


class TestJobTrackerPathExcludesTemporalQuery:
    def test_temporal_query_in_tracker_exclusion_list(self, dashboard_service):
        mock_tracker = MagicMock()
        mock_tracker.get_recent_jobs.return_value = []

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=MagicMock()
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=mock_tracker
            ):
                dashboard_service._get_recent_jobs("admin", "30d")

        _, kwargs = mock_tracker.get_recent_jobs.call_args
        assert "temporal_query" in kwargs["exclude_operation_types"]
        # Existing xray entries must still be present (no regression).
        assert "xray_search" in kwargs["exclude_operation_types"]
        assert "xray_explore" in kwargs["exclude_operation_types"]
        assert "xray_search_batch" in kwargs["exclude_operation_types"]


class TestFallbackPathExcludesTemporalQuery:
    def test_fallback_call_passes_exclude_operation_types(self, dashboard_service):
        """Previously the no-JobTracker fallback path called
        get_recent_jobs_with_filter with NO exclusion kwarg at all -- the
        exact gap the locked design's MEDIUM item calls out."""
        mock_mgr = MagicMock()
        mock_mgr.get_recent_jobs_with_filter.return_value = []

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(dashboard_service, "_get_job_tracker", return_value=None):
                dashboard_service._get_recent_jobs("admin", "30d")

        _, kwargs = mock_mgr.get_recent_jobs_with_filter.call_args
        assert "exclude_operation_types" in kwargs
        assert "temporal_query" in kwargs["exclude_operation_types"]
        assert "xray_search" in kwargs["exclude_operation_types"]
        assert "xray_explore" in kwargs["exclude_operation_types"]
        assert "xray_search_batch" in kwargs["exclude_operation_types"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
