"""
Tests for Bug #836: lifecycle_backfill jobs display "Unknown" in Repository column.

These tests verify that dashboard_service.py correctly labels lifecycle_backfill
jobs with a synthetic "N repos" or "all repos" label when repo_alias is absent.
"""

from typing import cast
from unittest.mock import MagicMock, patch


def _build_job(operation_type: str, repo_alias=None, result=None, metadata=None):
    """Build a minimal job dict matching the shape read by get_recent_jobs."""
    return {
        "job_id": "test-job-1",
        "operation_type": operation_type,
        "repo_alias": repo_alias,
        "result": result or {},
        "metadata": metadata or {},
        "status": "completed",
        "completed_at": "2026-04-18T10:00:00",
    }


def _get_repo_name_via_service(job: dict) -> str:
    """
    Exercise the real dashboard_service.get_recent_jobs() path for a single job
    and return the repo_name that ends up in the RecentJob result.
    """
    from code_indexer.server.services.dashboard_service import DashboardService

    service = DashboardService.__new__(DashboardService)
    service._container = MagicMock()

    mock_job_manager = MagicMock()
    mock_job_manager.get_recent_jobs_with_filter.return_value = [job]

    with (
        patch.object(
            service,
            "_get_background_job_manager",
            return_value=mock_job_manager,
        ),
        patch.object(
            service,
            "_get_job_tracker",
            return_value=None,  # force the job_manager code path
        ),
    ):
        recent = service._get_recent_jobs("admin")

    assert len(recent) == 1, f"Expected 1 job, got {len(recent)}"
    # cast needed: MagicMock attribute access (.repo_name) returns Any
    return cast(str, recent[0].repo_name)


class TestLifecycleBackfillLabel:
    """Bug #836: lifecycle_backfill repo column should show a meaningful label."""

    def test_lifecycle_backfill_with_total_shows_count(self):
        """Job with operation_type=lifecycle_backfill + cluster_wide_total=646 -> '646 repos'."""
        job = _build_job(
            operation_type="lifecycle_backfill",
            metadata={"cluster_wide_total": 646},
        )
        label = _get_repo_name_via_service(job)
        assert label == "646 repos"

    def test_lifecycle_backfill_without_total_shows_all_repos(self):
        """Job with operation_type=lifecycle_backfill but no cluster_wide_total -> 'all repos'."""
        job = _build_job(
            operation_type="lifecycle_backfill",
            metadata={},
        )
        label = _get_repo_name_via_service(job)
        assert label == "all repos"

    def test_lifecycle_backfill_with_explicit_alias_uses_alias(self):
        """If repo_alias IS set, it wins over any synthetic label -- backward compat."""
        job = _build_job(
            operation_type="lifecycle_backfill",
            repo_alias="my-repo",
            metadata={"cluster_wide_total": 646},
        )
        label = _get_repo_name_via_service(job)
        assert label == "my-repo"

    def test_other_job_without_alias_still_unknown(self):
        """sync_repo job with no alias must still show 'Unknown' -- no regression."""
        job = _build_job(
            operation_type="sync_repo",
        )
        label = _get_repo_name_via_service(job)
        assert label == "Unknown"

    def test_other_job_with_alias_works(self):
        """Regular job with valid repo_alias shows the alias -- no regression."""
        job = _build_job(
            operation_type="sync_repo",
            repo_alias="my-other-repo",
        )
        label = _get_repo_name_via_service(job)
        assert label == "my-other-repo"
