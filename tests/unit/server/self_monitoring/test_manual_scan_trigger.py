"""
Tests for Manual Scan Trigger (Story #75).

Tests the manual "Run Now" functionality including:
- AC2: Scan job submission via trigger_scan method
"""

from unittest.mock import Mock


def test_trigger_scan_submits_job_when_configured():
    """Test that trigger_scan submits a self_monitoring job when job_manager is available."""
    from code_indexer.server.self_monitoring.service import SelfMonitoringService

    # Arrange
    job_manager = Mock()
    job_manager.submit_job = Mock(return_value="scan-job-001")

    service = SelfMonitoringService(
        enabled=True,
        cadence_minutes=60,
        job_manager=job_manager
    )

    # Act
    result = service.trigger_scan()

    # Assert
    assert result["status"] == "queued"
    assert result["scan_id"] == "scan-job-001"
    job_manager.submit_job.assert_called_once()

    # Verify the job was tagged as self_monitoring
    call_kwargs = job_manager.submit_job.call_args[1]
    assert call_kwargs["operation_type"] == "self_monitoring"
    assert call_kwargs["submitter_username"] == "system"
    assert call_kwargs["is_admin"] is True


def test_trigger_scan_returns_error_when_job_manager_missing():
    """Test that trigger_scan returns error when BackgroundJobManager is not initialized."""
    from code_indexer.server.self_monitoring.service import SelfMonitoringService

    # Arrange
    service = SelfMonitoringService(
        enabled=True,
        cadence_minutes=60,
        job_manager=None
    )

    # Act
    result = service.trigger_scan()

    # Assert
    assert result["status"] == "error"
    assert "job manager not available" in result["error"].lower()


def test_trigger_scan_returns_error_when_not_enabled():
    """Test that trigger_scan returns error when self-monitoring is disabled."""
    from code_indexer.server.self_monitoring.service import SelfMonitoringService

    # Arrange
    job_manager = Mock()
    service = SelfMonitoringService(
        enabled=False,
        cadence_minutes=60,
        job_manager=job_manager
    )

    # Act
    result = service.trigger_scan()

    # Assert
    assert result["status"] == "error"
    assert "not enabled" in result["error"].lower()
    job_manager.submit_job.assert_not_called()
