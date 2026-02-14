"""
Unit tests for DependencyMapService scheduler daemon thread (Story #193).

Tests daemon thread functionality:
- Daemon thread launch and lifecycle
- 60s polling interval
- Runtime configuration checks (dependency_map_enabled toggle)
- Concurrency protection with full analysis
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


@pytest.fixture
def mock_config_manager():
    """Create mock config manager with scheduler enabled."""
    config_manager = Mock()
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
    )
    config_manager.get_claude_integration_config.return_value = config
    return config_manager


@pytest.fixture
def mock_tracking_backend():
    """Create mock tracking backend."""
    backend = Mock()
    backend.get_tracking.return_value = {
        "id": 1,
        "last_run": "2024-01-01T00:00:00Z",
        "next_run": "2024-01-02T00:00:00Z",
        "status": "completed",
        "commit_hashes": None,
        "error_message": None,
    }
    return backend


@pytest.fixture
def mock_golden_repos_manager(tmp_path):
    """Create mock golden repos manager."""
    manager = Mock()
    manager.golden_repos_dir = tmp_path / "golden-repos"
    manager.golden_repos_dir.mkdir()
    manager.list_golden_repos.return_value = []
    return manager


@pytest.fixture
def dependency_map_service(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
):
    """Create DependencyMapService instance."""
    return DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=Mock(),
    )


class TestSchedulerDaemonThread:
    """Test daemon thread scheduler with 60s polling (AC1)."""

    def test_start_scheduler_launches_daemon_thread(
        self, dependency_map_service
    ):
        """Test that start_scheduler launches daemon thread."""
        try:
            dependency_map_service.start_scheduler()

            # Check thread is running
            assert dependency_map_service._daemon_thread is not None
            assert dependency_map_service._daemon_thread.is_alive()
            assert dependency_map_service._daemon_thread.daemon is True

        finally:
            # Stop scheduler
            dependency_map_service.stop_scheduler()

    def test_scheduler_respects_enabled_toggle(
        self, dependency_map_service, mock_config_manager, mock_tracking_backend
    ):
        """Test that scheduler checks dependency_map_enabled at runtime (AC6)."""
        # Disable dependency map
        mock_config_manager.get_claude_integration_config.return_value.dependency_map_enabled = False

        # Set next_run to trigger immediately
        now = datetime.now(timezone.utc)
        mock_tracking_backend.get_tracking.return_value["next_run"] = (
            now - timedelta(hours=1)
        ).isoformat()

        # Mock run_delta_analysis to track calls
        with patch.object(dependency_map_service, 'run_delta_analysis') as mock_delta:
            try:
                dependency_map_service.start_scheduler()

                # Give scheduler time to poll
                time.sleep(0.2)

                # run_delta_analysis should NOT have been called (feature disabled)
                mock_delta.assert_not_called()

            finally:
                dependency_map_service.stop_scheduler()

    def test_scheduler_triggers_on_next_run_time(
        self, dependency_map_service, mock_tracking_backend
    ):
        """Test that scheduler triggers delta analysis when next_run is reached."""
        # Set next_run to trigger immediately
        now = datetime.now(timezone.utc)
        mock_tracking_backend.get_tracking.return_value["next_run"] = (
            now - timedelta(hours=1)
        ).isoformat()

        # Mock run_delta_analysis to avoid actual execution
        with patch.object(dependency_map_service, 'run_delta_analysis') as mock_delta:
            try:
                dependency_map_service.start_scheduler()

                # Give scheduler time to poll and trigger
                time.sleep(0.5)

                # run_delta_analysis should have been called
                mock_delta.assert_called()

            finally:
                dependency_map_service.stop_scheduler()

    def test_stop_scheduler_sets_stop_event(self, dependency_map_service):
        """Test that stop_scheduler sets the stop event and stops thread."""
        dependency_map_service.start_scheduler()

        assert dependency_map_service._daemon_thread.is_alive()

        dependency_map_service.stop_scheduler()

        # Thread should stop within timeout
        dependency_map_service._daemon_thread.join(timeout=2.0)
        assert not dependency_map_service._daemon_thread.is_alive()


class TestConcurrencyProtection:
    """Test concurrency protection between delta and full analysis (AC7)."""

    def test_delta_analysis_skips_if_full_analysis_running(
        self, dependency_map_service
    ):
        """Test that delta analysis skips if full analysis holds the lock."""
        # Acquire lock (simulating full analysis running)
        dependency_map_service._lock.acquire()

        try:
            # Attempt delta analysis (should skip without blocking)
            result = dependency_map_service.run_delta_analysis()

            # Should return early with skip message or None
            # (Implementation will determine exact return value)
            assert result is None or result.get("status") == "skipped"

        finally:
            dependency_map_service._lock.release()

    def test_full_analysis_fails_if_delta_analysis_running(
        self, dependency_map_service
    ):
        """Test that full analysis fails if delta analysis holds the lock."""
        # Acquire lock (simulating delta analysis running)
        dependency_map_service._lock.acquire()

        try:
            # Attempt full analysis (should fail immediately)
            with pytest.raises(RuntimeError, match="already in progress"):
                dependency_map_service.run_full_analysis()

        finally:
            dependency_map_service._lock.release()
