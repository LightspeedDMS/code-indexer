"""Unit tests for Bug #469 Fix 3: Rollback in change_branch on partial failure.

Tests verify that when any step AFTER _cb_checkout_and_pull fails, the
git HEAD is rolled back to the previous branch before re-raising.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path):
    """Return a temp data directory with the golden-repos sub-directory."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "golden-repos").mkdir()
    return str(d)


@pytest.fixture
def manager(data_dir):
    """GoldenRepoManager pre-populated with one golden repo on branch 'main'."""
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
# Rollback behaviour tests
# ---------------------------------------------------------------------------


class TestChangeBranchRollback:
    """Verify that a failure after checkout triggers a git rollback."""

    def test_rollback_called_when_cidx_index_fails(self, manager):
        """When _cb_cidx_index raises, git checkout <previous_branch> is run."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(
                manager, "_cb_cidx_index", side_effect=RuntimeError("index failed")
            ),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run"
            ) as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(RuntimeError, match="index failed"):
                manager.change_branch("my-repo", "feature-x")

        mock_run.assert_called_once_with(
            ["git", "checkout", "main"],
            cwd="/golden-repos/my-repo",
            check=True,
            capture_output=True,
        )

    def test_rollback_called_when_cow_snapshot_fails(self, manager):
        """When _cb_cow_snapshot raises, git checkout <previous_branch> is run."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(
                manager,
                "_cb_cow_snapshot",
                side_effect=RuntimeError("snapshot failed"),
            ),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run"
            ) as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(RuntimeError, match="snapshot failed"):
                manager.change_branch("my-repo", "feature-x")

        mock_run.assert_called_once_with(
            ["git", "checkout", "main"],
            cwd="/golden-repos/my-repo",
            check=True,
            capture_output=True,
        )

    def test_original_exception_reraised_when_rollback_fails(self, manager):
        """When rollback itself fails, original exception is still re-raised."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(
                manager, "_cb_cidx_index", side_effect=RuntimeError("index failed")
            ),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
        ):
            with pytest.raises(RuntimeError, match="index failed"):
                manager.change_branch("my-repo", "feature-x")

    def test_rollback_error_is_logged_when_rollback_fails(self, manager):
        """When rollback itself fails, the rollback error is logged."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(
                manager, "_cb_cidx_index", side_effect=RuntimeError("index failed")
            ),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.logger"
            ) as mock_logger,
        ):
            with pytest.raises(RuntimeError):
                manager.change_branch("my-repo", "feature-x")

        mock_logger.error.assert_called_once()
        error_call_args = mock_logger.error.call_args[0]
        assert "Rollback" in error_call_args[0]

    def test_successful_branch_change_no_rollback(self, manager):
        """On success, subprocess.run is NOT called for rollback."""
        with (
            patch.object(manager, "_cb_git_fetch_and_validate"),
            patch.object(manager, "_cb_checkout_and_pull"),
            patch.object(manager, "_cb_cidx_index"),
            patch.object(manager, "_cb_cow_snapshot", return_value="/snap/v_1"),
            patch.object(manager, "_cb_fts_branch_cleanup"),
            patch.object(manager, "_cb_hnsw_branch_cleanup"),
            patch.object(manager, "_cb_swap_alias"),
            patch(
                "code_indexer.server.repositories.golden_repo_manager.subprocess.run"
            ) as mock_run,
        ):
            result = manager.change_branch("my-repo", "feature-x")

        assert result["success"] is True
        mock_run.assert_not_called()
