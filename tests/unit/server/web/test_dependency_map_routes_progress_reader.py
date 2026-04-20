"""
Unit tests for _get_progress_from_service in dependency_map_routes.

TDD Red phase: verifies that 'lifecycle_backfill' is accepted by the
operation_type allowlist.  Before the fix the allowlist only covers
dependency_map_* types, so lifecycle_backfill jobs are silently skipped
and the function returns (0, "") even when an active lifecycle_backfill
job is running.

Tests:
  1. lifecycle_backfill active job returns its progress and progress_info.
  2. Existing dependency_map_full active job still works (regression guard).
  3. Returns (0, "") when dep_map_service is None.
"""

from unittest.mock import MagicMock, Mock

from code_indexer.server.web.dependency_map_routes import _get_progress_from_service

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

_LIFECYCLE_BACKFILL_PROGRESS = 42
_LIFECYCLE_BACKFILL_INFO = "2/4 repos processed"

_DEPMAP_FULL_PROGRESS = 75
_DEPMAP_FULL_INFO = "3/4 repos analyzed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_with_job(operation_type: str, progress: int, progress_info: str):
    """Build a fake dep_map_service whose tracker returns a single active job."""
    active_job = Mock()
    active_job.operation_type = operation_type
    active_job.progress = progress
    active_job.progress_info = progress_info

    tracker = MagicMock()
    tracker.get_active_jobs.return_value = [active_job]

    service = MagicMock()
    service._job_tracker = tracker
    return service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetProgressFromServiceLifecycleBackfill:
    """_get_progress_from_service must surface lifecycle_backfill jobs."""

    def test_lifecycle_backfill_job_returns_progress_and_info(self):
        """
        When the active job has operation_type='lifecycle_backfill',
        _get_progress_from_service must return (progress, progress_info).

        Bug: before the fix, 'lifecycle_backfill' is absent from the allowlist
        so this returns (0, "") instead.
        """
        service = _make_service_with_job(
            operation_type="lifecycle_backfill",
            progress=_LIFECYCLE_BACKFILL_PROGRESS,
            progress_info=_LIFECYCLE_BACKFILL_INFO,
        )

        result = _get_progress_from_service(service)

        assert result == (_LIFECYCLE_BACKFILL_PROGRESS, _LIFECYCLE_BACKFILL_INFO), (
            f"Expected ({_LIFECYCLE_BACKFILL_PROGRESS!r}, {_LIFECYCLE_BACKFILL_INFO!r}), "
            f"got {result!r}. "
            "Bug: 'lifecycle_backfill' is missing from the operation_type allowlist."
        )

    def test_dependency_map_full_job_still_works_regression_guard(self):
        """
        Existing dependency_map_full jobs must continue to be surfaced
        after adding lifecycle_backfill to the allowlist.
        """
        service = _make_service_with_job(
            operation_type="dependency_map_full",
            progress=_DEPMAP_FULL_PROGRESS,
            progress_info=_DEPMAP_FULL_INFO,
        )

        result = _get_progress_from_service(service)

        assert result == (_DEPMAP_FULL_PROGRESS, _DEPMAP_FULL_INFO), (
            f"dependency_map_full regression: expected "
            f"({_DEPMAP_FULL_PROGRESS!r}, {_DEPMAP_FULL_INFO!r}), got {result!r}."
        )

    def test_returns_zero_progress_when_service_is_none(self):
        """_get_progress_from_service returns (0, '') when service is None."""
        result = _get_progress_from_service(None)

        assert result == (0, ""), (
            f"Expected (0, '') when service is None, got {result!r}."
        )
