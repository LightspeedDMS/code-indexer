"""
Unit tests for Story #311: DashboardService rewiring to read from JobTracker.

Epic #261 Story 1B.

Tests are written FIRST (TDD) to define expected behavior before implementation:

AC5: DashboardService._get_job_counts() reads active/pending from JobTracker
AC6: DashboardService._get_recent_jobs() reads from JobTracker when available
AC7: DashboardService._get_job_tracker() method exists and returns None gracefully
     when job_tracker is not set in app module
AC8: DashboardService falls back to BackgroundJobManager when tracker is None
"""

import sqlite3

import pytest
from unittest.mock import MagicMock, patch

from code_indexer.server.services.job_tracker import JobTracker


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite database with the full background_jobs schema."""
    db = tmp_path / "test_dash_tracker.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER NOT NULL DEFAULT 0,
        claude_actions TEXT,
        failure_reason TEXT,
        extended_error TEXT,
        language_resolution_status TEXT,
        progress_info TEXT,
        metadata TEXT
    )"""
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def tracker(db_path):
    """Create a real JobTracker connected to the temporary database."""
    return JobTracker(db_path)


@pytest.fixture
def dashboard_service():
    """Create a DashboardService instance."""
    from code_indexer.server.services.dashboard_service import DashboardService

    return DashboardService()


def _make_mock_job_manager(completed=5, failed=1, running=0, pending=0):
    """Build a MagicMock BackgroundJobManager with preset return values."""
    mock_mgr = MagicMock()
    mock_mgr.get_job_stats_with_filter.return_value = {
        "completed": completed,
        "failed": failed,
    }
    mock_mgr.get_active_job_count.return_value = running
    mock_mgr.get_pending_job_count.return_value = pending
    return mock_mgr


# ---------------------------------------------------------------------------
# AC5: DashboardService._get_job_tracker method exists
# ---------------------------------------------------------------------------


class TestDashboardServiceGetJobTracker:
    """DashboardService must expose _get_job_tracker() method."""

    def test_dashboard_service_has_get_job_tracker_method(self, dashboard_service):
        """
        DashboardService has a _get_job_tracker() method.

        Given a DashboardService instance
        When checking for _get_job_tracker attribute
        Then the method exists and is callable
        """
        assert hasattr(dashboard_service, "_get_job_tracker")
        assert callable(dashboard_service._get_job_tracker)

    def test_get_job_tracker_returns_none_when_not_in_app(self, dashboard_service):
        """
        _get_job_tracker returns None when job_tracker is None in the app module.

        Given a DashboardService instance
        And the app module has job_tracker set to None
        When _get_job_tracker is called
        Then None is returned without raising any exception
        """
        # Explicitly patch job_tracker to None for test isolation.
        # app.py now declares job_tracker = None at module level, so previous
        # tests using patch(..., create=True) can contaminate the module state.
        with patch("code_indexer.server.app.job_tracker", None):
            result = dashboard_service._get_job_tracker()
        assert result is None

    def test_get_job_tracker_returns_tracker_when_set_in_app(
        self, dashboard_service, tracker
    ):
        """
        _get_job_tracker returns the tracker when app module has job_tracker.

        Given a DashboardService instance
        And the app module exposes a job_tracker attribute
        When _get_job_tracker is called
        Then the tracker is returned
        """
        with patch("code_indexer.server.app.job_tracker", tracker, create=True):
            result = dashboard_service._get_job_tracker()

        assert result is tracker


# ---------------------------------------------------------------------------
# AC5/AC6: _get_job_counts reads from tracker when available
# ---------------------------------------------------------------------------


class TestDashboardJobCountsWithTracker:
    """_get_job_counts must read active/pending from JobTracker when available."""

    def test_get_job_counts_uses_tracker_pending_count(
        self, dashboard_service, tracker
    ):
        """
        _get_job_counts reads pending count from job_tracker.

        Given a JobTracker with 2 pending jobs
        When _get_job_counts is called
        Then queued count reflects the tracker's pending count (2)
        """
        tracker.register_job("dash-j001", "test_op", "admin")
        tracker.register_job("dash-j002", "test_op", "admin")
        # Both remain pending

        mock_mgr = _make_mock_job_manager(completed=5, failed=1, running=0, pending=0)

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                counts = dashboard_service._get_job_counts("admin", "24h")

        # Tracker says 0 running, 2 pending
        assert counts.running == 0
        assert counts.queued == 2

    def test_get_job_counts_uses_tracker_running_count(
        self, dashboard_service, tracker
    ):
        """
        _get_job_counts reads running count from job_tracker.

        Given a JobTracker with 1 running job and 1 pending job
        When _get_job_counts is called
        Then running=1 and queued=1
        """
        tracker.register_job("dash-run-001", "test_op", "admin")
        tracker.update_status("dash-run-001", status="running")
        tracker.register_job("dash-pend-001", "test_op", "admin")
        # dash-pend-001 stays pending

        mock_mgr = _make_mock_job_manager(completed=3, failed=0)

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                counts = dashboard_service._get_job_counts("admin", "24h")

        assert counts.running == 1
        assert counts.queued == 1

    def test_get_job_counts_still_reads_completed_failed_from_job_manager(
        self, dashboard_service, tracker
    ):
        """
        _get_job_counts reads completed/failed stats from job_manager even with tracker.

        Given a tracker and a job_manager with completed=7, failed=2
        When _get_job_counts is called
        Then completed_24h=7 and failed_24h=2 (from job_manager stats)
        """
        mock_mgr = _make_mock_job_manager(completed=7, failed=2)

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                counts = dashboard_service._get_job_counts("admin", "24h")

        assert counts.completed_24h == 7
        assert counts.failed_24h == 2

    def test_get_job_counts_falls_back_to_job_manager_when_no_tracker(
        self, dashboard_service
    ):
        """
        _get_job_counts falls back to job_manager when tracker is None.

        Given no job_tracker available (returns None)
        When _get_job_counts is called
        Then it reads running/pending from the background job manager
        """
        mock_mgr = _make_mock_job_manager(
            completed=3, failed=0, running=1, pending=2
        )

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=None
            ):
                counts = dashboard_service._get_job_counts("admin", "24h")

        assert counts.running == 1
        assert counts.queued == 2


# ---------------------------------------------------------------------------
# AC6/AC7: _get_recent_jobs reads from tracker when available
# ---------------------------------------------------------------------------


class TestDashboardRecentJobsWithTracker:
    """_get_recent_jobs must read from JobTracker when available."""

    def test_get_recent_jobs_uses_tracker(self, dashboard_service, tracker):
        """
        _get_recent_jobs reads from job_tracker when available.

        Given a JobTracker with a completed job
        When _get_recent_jobs is called with the tracker
        Then the completed job appears in the result list
        """
        tracker.register_job(
            "dash-recent-001",
            "dep_map_analysis",
            "admin",
            repo_alias="my-repo",
        )
        tracker.update_status("dash-recent-001", status="running")
        tracker.complete_job("dash-recent-001", result={"ok": True})

        mock_mgr = MagicMock()
        mock_mgr.get_recent_jobs_with_filter.return_value = []

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                recent = dashboard_service._get_recent_jobs("admin", "30d")

        assert isinstance(recent, list)
        job_ids = [r.job_id for r in recent]
        assert "dash-recent-001" in job_ids

    def test_get_recent_jobs_includes_repo_alias_from_tracker(
        self, dashboard_service, tracker
    ):
        """
        _get_recent_jobs uses repo_alias from tracker-sourced job data.

        Given a job registered with repo_alias='specific-repo'
        When _get_recent_jobs retrieves it via the tracker
        Then the resulting RecentJob has repo_name='specific-repo'
        """
        tracker.register_job(
            "dash-alias-001",
            "refresh_golden_repo",
            "admin",
            repo_alias="specific-repo",
        )
        tracker.update_status("dash-alias-001", status="running")
        tracker.complete_job("dash-alias-001")

        mock_mgr = MagicMock()
        mock_mgr.get_recent_jobs_with_filter.return_value = []

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                recent = dashboard_service._get_recent_jobs("admin", "30d")

        matching = [r for r in recent if r.job_id == "dash-alias-001"]
        assert len(matching) == 1
        assert matching[0].repo_name == "specific-repo"

    def test_get_recent_jobs_falls_back_when_no_tracker(self, dashboard_service):
        """
        _get_recent_jobs falls back to job_manager when tracker is None.

        Given no job_tracker available
        When _get_recent_jobs is called
        Then it uses the background job manager's get_recent_jobs_with_filter
        """
        mock_mgr = MagicMock()
        mock_mgr.get_recent_jobs_with_filter.return_value = [
            {
                "job_id": "fallback-job-001",
                "operation_type": "test_op",
                "status": "completed",
                "completed_at": "2024-01-01T10:00:00+00:00",
                "repo_alias": "my-repo",
            }
        ]

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=None
            ):
                recent = dashboard_service._get_recent_jobs("admin", "30d")

        assert isinstance(recent, list)
        assert len(recent) >= 1
        # Job manager method was called (not tracker)
        mock_mgr.get_recent_jobs_with_filter.assert_called_once()

    def test_get_recent_jobs_does_not_call_job_manager_when_tracker_available(
        self, dashboard_service, tracker
    ):
        """
        When tracker is available, get_recent_jobs_with_filter is NOT called.

        Given a tracker is configured
        When _get_recent_jobs is called
        Then the job_manager's get_recent_jobs_with_filter is NOT invoked
        """
        tracker.register_job("dash-notcalled-001", "test_op", "admin")
        tracker.update_status("dash-notcalled-001", status="running")
        tracker.complete_job("dash-notcalled-001")

        mock_mgr = MagicMock()
        mock_mgr.get_recent_jobs_with_filter.return_value = []

        with patch.object(
            dashboard_service, "_get_background_job_manager", return_value=mock_mgr
        ):
            with patch.object(
                dashboard_service, "_get_job_tracker", return_value=tracker
            ):
                dashboard_service._get_recent_jobs("admin", "30d")

        # When tracker is available, the BGM's method should NOT be called
        mock_mgr.get_recent_jobs_with_filter.assert_not_called()
