"""
Unit tests for Story #4 AC3: Dashboard API Metrics Display.

Tests that API metrics are passed to the dashboard template and displayed correctly.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
from unittest.mock import MagicMock, patch


@pytest.mark.slow
class TestDashboardApiMetricsIntegration:
    """Test AC3: API metrics appear in dashboard stats."""

    def test_dashboard_stats_includes_api_metrics(self):
        """Test that get_stats_partial includes API metrics in returned data."""
        from src.code_indexer.server.services.dashboard_service import DashboardService
        import src.code_indexer.server.services.api_metrics_service as ams_module

        expected_metrics = {
            "semantic_searches": 2,
            "other_index_searches": 1,
            "regex_searches": 1,
            "other_api_calls": 0,
        }

        # Create dashboard service
        service = DashboardService()

        # Mock the job and repo managers
        mock_job_manager = MagicMock()
        mock_job_manager.get_job_stats_with_filter.return_value = {
            "completed": 5,
            "failed": 1,
        }
        mock_job_manager.get_active_job_count.return_value = 0
        mock_job_manager.get_pending_job_count.return_value = 0
        mock_job_manager.get_recent_jobs_with_filter.return_value = []

        mock_golden_manager = MagicMock()
        mock_golden_manager.list_golden_repos.return_value = []

        mock_activated_manager = MagicMock()
        mock_activated_manager.list_activated_repositories.return_value = []

        # Patch get_metrics_bucketed — production code reads from DB buckets, not
        # in-memory counters, so patching this is the correct integration seam.
        with (
            patch.object(
                service, "_get_background_job_manager", return_value=mock_job_manager
            ),
            patch.object(
                service, "_get_golden_repo_manager", return_value=mock_golden_manager
            ),
            patch.object(
                service,
                "_get_activated_repo_manager",
                return_value=mock_activated_manager,
            ),
            patch.object(
                ams_module.api_metrics_service,
                "get_metrics_bucketed",
                return_value=expected_metrics,
            ),
        ):
            stats_data = service.get_stats_partial("testuser", "24h", "30d")

        # AC3: Stats data should include API metrics
        assert "api_metrics" in stats_data, "get_stats_partial must include api_metrics"

        api_metrics = stats_data["api_metrics"]
        assert api_metrics["semantic_searches"] == 2
        assert api_metrics["other_index_searches"] == 1
        assert api_metrics["regex_searches"] == 1
        assert api_metrics["other_api_calls"] == 0

    def test_api_metrics_reset_returns_zeros(self):
        """Test that API metrics return zeros after reset."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        # Add some counts
        api_metrics_service.increment_semantic_search()
        api_metrics_service.increment_regex_search()

        # Reset
        api_metrics_service.reset()

        # Get metrics
        metrics = api_metrics_service.get_metrics()

        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0
        assert metrics["other_api_calls"] == 0

    def test_api_metrics_increment_other_api_calls(self):
        """Test that other API calls counter works correctly."""
        from src.code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )

        api_metrics_service.reset()

        # Increment other API calls
        api_metrics_service.increment_other_api_call()
        api_metrics_service.increment_other_api_call()
        api_metrics_service.increment_other_api_call()

        metrics = api_metrics_service.get_metrics()

        assert metrics["other_api_calls"] == 3
