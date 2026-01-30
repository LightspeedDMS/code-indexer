"""
Unit tests for Bug Fix: Dashboard uses repo_alias as primary source for repo name.

Tests that _get_recent_jobs() in dashboard_service.py checks job["repo_alias"]
FIRST before falling back to result dict values, so running/pending jobs
(which have empty result) can still display their repository name.

Root Cause:
- _get_recent_jobs() only checked result dict for repo name
- Running/pending jobs have empty result => "Unknown"
- repo_alias was available but not used

Fix:
- Check job["repo_alias"] FIRST
- Only fall back to result dict if repo_alias is None/empty

Following TDD methodology: Write failing tests FIRST, then implement.
"""

from unittest.mock import MagicMock, patch


from src.code_indexer.server.services.dashboard_service import DashboardService


class TestDashboardRepoAliasPriority:
    """Test that dashboard service uses repo_alias as primary source."""

    def test_running_job_uses_repo_alias_not_result(self):
        """Test that running jobs use repo_alias (result is empty).

        This is the core bug fix: running jobs have empty result, so
        the service must use repo_alias to get the repository name.
        """
        # Create mock job data as returned by get_recent_jobs_with_filter
        mock_job_data = [
            {
                "job_id": "running-job-1",
                "operation_type": "add_golden_repo",
                "status": "running",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": "2025-01-21T10:01:00+00:00",
                "completed_at": None,
                "progress": 50,
                "result": None,  # Running jobs have empty result
                "error": None,
                "username": "testuser",
                "repo_alias": "my-running-repo",  # This should be used
            }
        ]

        # Mock the BackgroundJobManager
        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        # Create service and mock _get_background_job_manager
        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        # THE BUG FIX: repo_name should come from repo_alias, not result
        assert recent_jobs[0].repo_name == "my-running-repo"
        assert recent_jobs[0].repo_name != "Unknown"

    def test_pending_job_uses_repo_alias_not_result(self):
        """Test that pending jobs use repo_alias (result is empty).

        Pending jobs also have empty result, so must use repo_alias.
        """
        mock_job_data = [
            {
                "job_id": "pending-job-1",
                "operation_type": "refresh_golden_repo",
                "status": "pending",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": None,
                "completed_at": None,
                "progress": 0,
                "result": None,  # Pending jobs have empty result
                "error": None,
                "username": "testuser",
                "repo_alias": "my-pending-repo",  # This should be used
            }
        ]

        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        assert recent_jobs[0].repo_name == "my-pending-repo"
        assert recent_jobs[0].repo_name != "Unknown"

    def test_completed_job_prefers_repo_alias_over_result(self):
        """Test that completed jobs also prefer repo_alias when available.

        Even though completed jobs have result dict, repo_alias should
        take priority for consistency.
        """
        mock_job_data = [
            {
                "job_id": "completed-job-1",
                "operation_type": "add_golden_repo",
                "status": "completed",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": "2025-01-21T10:01:00+00:00",
                "completed_at": "2025-01-21T10:05:00+00:00",
                "progress": 100,
                "result": {"alias": "result-alias"},  # Has result
                "error": None,
                "username": "testuser",
                "repo_alias": "job-repo-alias",  # Should take priority
            }
        ]

        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        # repo_alias should take priority over result["alias"]
        assert recent_jobs[0].repo_name == "job-repo-alias"

    def test_job_without_repo_alias_falls_back_to_result_alias(self):
        """Test fallback to result["alias"] when repo_alias is None.

        For backward compatibility, if repo_alias is not set, fall back
        to extracting the name from result dict.
        """
        mock_job_data = [
            {
                "job_id": "completed-job-1",
                "operation_type": "add_golden_repo",
                "status": "completed",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": "2025-01-21T10:01:00+00:00",
                "completed_at": "2025-01-21T10:05:00+00:00",
                "progress": 100,
                "result": {"alias": "fallback-alias"},
                "error": None,
                "username": "testuser",
                "repo_alias": None,  # Not set, should fallback
            }
        ]

        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        # Should fallback to result["alias"]
        assert recent_jobs[0].repo_name == "fallback-alias"

    def test_job_without_repo_alias_falls_back_to_result_user_alias(self):
        """Test fallback to result["user_alias"] when repo_alias is None.

        For activated repositories, the result may have user_alias instead.
        """
        mock_job_data = [
            {
                "job_id": "completed-job-1",
                "operation_type": "activate_repository",
                "status": "completed",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": "2025-01-21T10:01:00+00:00",
                "completed_at": "2025-01-21T10:05:00+00:00",
                "progress": 100,
                "result": {"user_alias": "my-activated-repo"},
                "error": None,
                "username": "testuser",
                "repo_alias": None,  # Not set, should fallback
            }
        ]

        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        # Should fallback to result["user_alias"]
        assert recent_jobs[0].repo_name == "my-activated-repo"

    def test_job_without_any_repo_info_shows_unknown(self):
        """Test that jobs without any repo info show "Unknown".

        When both repo_alias and result are empty/None, show "Unknown".
        """
        mock_job_data = [
            {
                "job_id": "mystery-job-1",
                "operation_type": "some_operation",
                "status": "running",
                "created_at": "2025-01-21T10:00:00+00:00",
                "started_at": "2025-01-21T10:01:00+00:00",
                "completed_at": None,
                "progress": 25,
                "result": None,
                "error": None,
                "username": "testuser",
                "repo_alias": None,  # No alias
            }
        ]

        mock_job_manager = MagicMock()
        mock_job_manager.get_recent_jobs_with_filter.return_value = mock_job_data

        service = DashboardService()
        with patch.object(
            service, "_get_background_job_manager", return_value=mock_job_manager
        ):
            recent_jobs = service._get_recent_jobs("testuser", "24h")

        assert len(recent_jobs) == 1
        # Should show "Unknown" as last resort
        assert recent_jobs[0].repo_name == "Unknown"
