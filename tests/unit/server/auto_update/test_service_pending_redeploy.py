"""Tests for AutoUpdateService pending redeploy marker handling (Issue #154)."""

from code_indexer.server.auto_update.service import AutoUpdateService, ServiceState
from code_indexer.server.auto_update.deployment_executor import PENDING_REDEPLOY_MARKER
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest


@pytest.fixture
def service():
    """Create AutoUpdateService instance for testing."""
    svc = AutoUpdateService(
        repo_path=Path("/test/repo"),
        check_interval=60,
    )
    # Inject mock components
    svc.change_detector = Mock()
    svc.deployment_lock = Mock()
    svc.deployment_executor = Mock()
    return svc


class TestPollOncePendingRedeploy:
    """Tests for poll_once() with pending redeploy marker."""

    def test_marker_forces_deployment_skipping_change_detection(self, service):
        """Test marker triggers deployment without change detection, removes marker, skips lock."""
        # Mock the marker at the module level where it's imported
        mock_marker = MagicMock()
        mock_marker.exists.return_value = True

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            service.deployment_executor.execute.return_value = True

            service.poll_once()

        # Verify marker.exists() was called
        mock_marker.exists.assert_called_once()

        # Verify marker was removed
        mock_marker.unlink.assert_called_once()

        # Verify deployment executed WITHOUT change detection
        service.deployment_executor.execute.assert_called_once()
        service.change_detector.has_changes.assert_not_called()

        # Verify lock not used (early return path)
        service.deployment_lock.acquire.assert_not_called()

        # Verify state remains IDLE (early return before state machine)
        assert service.current_state == ServiceState.IDLE

    def test_marker_removal_failure_does_not_block_deployment(self, service):
        """Test deployment proceeds even if marker removal fails."""
        mock_marker = MagicMock()
        mock_marker.exists.return_value = True
        mock_marker.unlink.side_effect = Exception("Permission denied")

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            service.deployment_executor.execute.return_value = True

            # Should not raise exception
            service.poll_once()

        # Deployment should still execute
        service.deployment_executor.execute.assert_called_once()

    def test_no_marker_follows_normal_change_detection_flow(self, service):
        """Test normal flow when marker absent: state machine runs, change detection happens."""
        mock_marker = MagicMock()
        mock_marker.exists.return_value = False

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            service.change_detector.has_changes.return_value = False

            service.poll_once()

        # Verify marker check happened
        mock_marker.exists.assert_called_once()

        # Verify normal flow: change detection runs
        service.change_detector.has_changes.assert_called_once()

        # No deployment (no changes)
        service.deployment_executor.execute.assert_not_called()

        # State machine ran (IDLE -> CHECKING -> IDLE)
        assert service.current_state == ServiceState.IDLE

    def test_marker_check_happens_before_state_transitions(self, service):
        """Test marker check precedes any state machine transitions."""
        call_order = []

        mock_marker = MagicMock()

        def mock_exists():
            call_order.append("marker_check")
            return False

        mock_marker.exists.side_effect = mock_exists

        def mock_transition(state):
            call_order.append(f"transition_{state.value}")

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            service.transition_to = mock_transition
            service.change_detector.has_changes.return_value = False

            service.poll_once()

        # Marker check must be first operation
        assert call_order[0] == "marker_check"
        assert call_order[1] == "transition_checking"

    def test_marker_path_constant_verified(self, service):
        """Test that PENDING_REDEPLOY_MARKER constant is the path checked."""
        # Verify the constant has the expected value
        # Note: Using /var/lib/ instead of /tmp/ because systemd PrivateTmp=yes isolates /tmp
        assert PENDING_REDEPLOY_MARKER == Path("/var/lib/cidx-pending-redeploy")

        # Verify poll_once uses the marker (functional test)
        mock_marker = MagicMock()
        mock_marker.exists.return_value = False

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            service.change_detector.has_changes.return_value = False

            # Should complete without error, confirming marker check integration
            service.poll_once()

        # Verify marker was checked
        mock_marker.exists.assert_called_once()

        # Verify normal flow completed
        service.change_detector.has_changes.assert_called_once()

    def test_marker_deployment_logs_forced_deployment(self, service):
        """Test that forced deployment due to marker is logged."""
        mock_marker = MagicMock()
        mock_marker.exists.return_value = True

        with patch("code_indexer.server.auto_update.service.PENDING_REDEPLOY_MARKER", mock_marker):
            with patch("code_indexer.server.auto_update.service.logger") as mock_logger:
                service.deployment_executor.execute.return_value = True

                service.poll_once()

        # Verify logging mentions pending/redeploy
        mock_logger.info.assert_called()
        log_calls = [str(call) for call in mock_logger.info.call_args_list]
        assert any("pending" in str(call).lower() or "redeploy" in str(call).lower() for call in log_calls)
