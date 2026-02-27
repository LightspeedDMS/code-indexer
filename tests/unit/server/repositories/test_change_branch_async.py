"""Tests for Story #308: GoldenRepoManager.change_branch_async().

Tests cover:
- change_branch_async() submits job to BackgroundJobManager with correct params
- change_branch_async() re-raises DuplicateJobError from submit_job
- change_branch_async() returns job_id=None when already on target branch
- change_branch_async() raises ValueError for invalid branch names
- change_branch_async() raises GoldenRepoNotFoundError for unknown alias
- The background_worker closure passed to submit_job calls change_branch()
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from code_indexer.server.repositories.background_jobs import DuplicateJobError


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    """Return a temp data directory."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "golden-repos").mkdir()
    return str(d)


@pytest.fixture
def manager(data_dir):
    """GoldenRepoManager with one golden repo on branch 'main'."""
    mgr = GoldenRepoManager(data_dir=data_dir)
    mgr.golden_repos["my-repo"] = GoldenRepo(
        alias="my-repo",
        repo_url="https://github.com/org/repo.git",
        default_branch="main",
        clone_path="/golden-repos/my-repo",
        created_at="2025-01-01T00:00:00Z",
    )
    mgr._sqlite_backend = MagicMock()
    mgr.resource_config = None
    return mgr


# ---------------------------------------------------------------------------
# TestChangeBranchAsync: tests for change_branch_async()
# ---------------------------------------------------------------------------


class TestChangeBranchAsync:
    """Tests for GoldenRepoManager.change_branch_async() (Story #308)."""

    def test_change_branch_async_submits_job_to_background_job_manager(self, manager):
        """change_branch_async() calls submit_job() with correct operation_type and alias."""
        mock_job_manager = MagicMock()
        mock_job_manager.submit_job.return_value = "job-abc-123"
        manager.background_job_manager = mock_job_manager

        result = manager.change_branch_async("my-repo", "feature-x", "admin")

        mock_job_manager.submit_job.assert_called_once()
        call_kwargs = mock_job_manager.submit_job.call_args
        assert call_kwargs.kwargs.get("operation_type") == "change_branch", (
            "submit_job must use operation_type='change_branch'"
        )
        assert call_kwargs.kwargs.get("repo_alias") == "my-repo", (
            "submit_job must pass repo_alias='my-repo'"
        )
        assert call_kwargs.kwargs.get("submitter_username") == "admin", (
            "submit_job must pass submitter_username='admin'"
        )
        assert result.get("job_id") == "job-abc-123", (
            "change_branch_async() must return dict with job_id from submit_job"
        )

    def test_change_branch_async_reraises_duplicate_job_error(self, manager):
        """change_branch_async() re-raises DuplicateJobError from submit_job."""
        mock_job_manager = MagicMock()
        duplicate_error = DuplicateJobError("change_branch", "my-repo", "existing-job-id")
        mock_job_manager.submit_job.side_effect = duplicate_error
        manager.background_job_manager = mock_job_manager

        with pytest.raises(DuplicateJobError) as exc_info:
            manager.change_branch_async("my-repo", "feature-x", "admin")

        assert exc_info.value.existing_job_id == "existing-job-id"

    def test_change_branch_async_returns_none_job_id_when_already_on_branch(
        self, manager
    ):
        """change_branch_async() returns job_id=None when already on target branch."""
        mock_job_manager = MagicMock()
        manager.background_job_manager = mock_job_manager

        result = manager.change_branch_async("my-repo", "main", "admin")

        assert result.get("job_id") is None, (
            "Should return job_id=None when already on target branch"
        )
        assert result.get("success") is True, (
            "Should return success=True when already on target branch"
        )
        mock_job_manager.submit_job.assert_not_called()

    def test_change_branch_async_raises_value_error_for_invalid_branch(self, manager):
        """change_branch_async() raises ValueError for invalid branch names."""
        mock_job_manager = MagicMock()
        manager.background_job_manager = mock_job_manager

        with pytest.raises(ValueError):
            manager.change_branch_async("my-repo", "invalid branch!", "admin")

        mock_job_manager.submit_job.assert_not_called()

    def test_change_branch_async_raises_not_found_for_unknown_alias(self, manager):
        """change_branch_async() raises GoldenRepoNotFoundError for unknown alias."""
        mock_job_manager = MagicMock()
        manager.background_job_manager = mock_job_manager

        with pytest.raises(GoldenRepoNotFoundError):
            manager.change_branch_async("nonexistent-repo", "feature-x", "admin")

        mock_job_manager.submit_job.assert_not_called()

    def test_change_branch_async_background_worker_calls_change_branch(self, manager):
        """The background_worker closure passed to submit_job calls change_branch()."""
        mock_job_manager = MagicMock()
        mock_job_manager.submit_job.return_value = "job-xyz"

        captured_func = {}

        def capture_func(**kwargs):
            captured_func["func"] = kwargs.get("func")
            return "job-xyz"

        mock_job_manager.submit_job.side_effect = capture_func
        manager.background_job_manager = mock_job_manager

        manager.change_branch_async("my-repo", "feature-x", "admin")

        assert "func" in captured_func, "submit_job must receive a func kwarg"

        # Execute the captured worker function and verify it calls change_branch
        with patch.object(manager, "change_branch", return_value={"success": True}) as mock_cb:
            captured_func["func"]()
            mock_cb.assert_called_once_with("my-repo", "feature-x")
