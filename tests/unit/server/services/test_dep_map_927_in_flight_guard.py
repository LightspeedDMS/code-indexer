"""Story #927: _is_any_dep_map_job_in_flight() re-entrance guard tests."""

import pytest
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import TrackedJob


_DEP_MAP_OP_TYPES = [
    "dependency_map_full",
    "dependency_map_delta",
    "dependency_map_refinement",
    "dependency_map_repair",
]


def _make_service(job_tracker=None):
    """Create DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    config_manager = MagicMock()
    tracking_backend = MagicMock()
    tracking_backend.get_tracking.return_value = {}
    analyzer = MagicMock()

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        job_tracker=job_tracker,
    )


def _make_tracked_job(operation_type: str, status: str) -> TrackedJob:
    """Build a minimal TrackedJob with the given operation_type and status."""
    return TrackedJob(
        job_id="test-job-id",
        operation_type=operation_type,
        status=status,
        username="admin",
    )


class TestIsAnyDepMapJobInFlight:
    def test_returns_false_when_no_jobs_in_flight(self):
        """Returns False when get_active_jobs returns an empty list."""
        mock_tracker = MagicMock()
        mock_tracker.get_active_jobs.return_value = []
        service = _make_service(job_tracker=mock_tracker)

        result = service._is_any_dep_map_job_in_flight()

        assert result is False

    def test_returns_true_when_full_job_pending(self):
        """Returns True when a dependency_map_full job is pending."""
        mock_tracker = MagicMock()
        job = _make_tracked_job("dependency_map_full", "pending")
        mock_tracker.get_active_jobs.return_value = [job]
        service = _make_service(job_tracker=mock_tracker)

        result = service._is_any_dep_map_job_in_flight()

        assert result is True

    def test_returns_true_when_repair_running(self):
        """Returns True when a dependency_map_repair job is running."""
        mock_tracker = MagicMock()
        job = _make_tracked_job("dependency_map_repair", "running")
        mock_tracker.get_active_jobs.return_value = [job]
        service = _make_service(job_tracker=mock_tracker)

        result = service._is_any_dep_map_job_in_flight()

        assert result is True

    @pytest.mark.parametrize("op_type", _DEP_MAP_OP_TYPES)
    def test_returns_true_for_each_dep_map_operation_type(self, op_type: str):
        """Returns True for every one of the 4 dep-map operation types."""
        mock_tracker = MagicMock()
        job = _make_tracked_job(op_type, "running")
        mock_tracker.get_active_jobs.return_value = [job]
        service = _make_service(job_tracker=mock_tracker)

        result = service._is_any_dep_map_job_in_flight()

        assert result is True, f"Expected True for op_type={op_type!r}"

    def test_returns_false_when_job_tracker_is_none(self):
        """Returns False (graceful) when _job_tracker is None."""
        service = _make_service(job_tracker=None)

        result = service._is_any_dep_map_job_in_flight()

        assert result is False
