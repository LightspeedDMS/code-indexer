"""Regression tests for Bug #1843 removal/refresh write coordination."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.repositories.golden_repo_manager import (
    GitOperationError,
    GoldenRepo,
    GoldenRepoManager,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def manager(tmp_path):
    manager = GoldenRepoManager(data_dir=str(tmp_path))
    DatabaseSchema(manager.db_path).initialize_database()
    manager.background_job_manager = MagicMock()
    manager.background_job_manager.submit_job.return_value = "remove-job-1843"
    manager.activated_repo_manager = None  # type: ignore[assignment]
    return manager


def _register_repo(manager, alias="race-repo"):
    clone_path = Path(manager.golden_repos_dir) / alias
    clone_path.mkdir(parents=True)
    repo = GoldenRepo(
        alias=alias,
        repo_url=f"https://example.test/{alias}.git",
        default_branch="main",
        clone_path=str(clone_path),
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    manager.golden_repos[alias] = repo
    manager._sqlite_backend.add_repo(
        alias=repo.alias,
        repo_url=repo.repo_url,
        default_branch=repo.default_branch,
        clone_path=repo.clone_path,
        created_at=repo.created_at,
        enable_temporal=False,
        temporal_options=None,
    )
    return clone_path


def _worker_for(manager, alias):
    manager.remove_golden_repo(alias)
    return manager.background_job_manager.submit_job.call_args.kwargs["func"]


def test_remove_refuses_active_refresh_before_cleanup(manager):
    """An active refresh must prevent the destructive cleanup step."""
    alias = "refresh-race-repo"
    clone_path = _register_repo(manager, alias)
    scheduler = MagicMock()
    scheduler.check_refresh_not_in_progress.side_effect = DuplicateJobError(
        "global_repo_refresh", f"{alias}-global", "refresh-job-1843"
    )
    manager._refresh_scheduler = scheduler

    worker = _worker_for(manager, alias)
    with patch.object(manager, "_cleanup_repository_files") as cleanup:
        with pytest.raises(GitOperationError, match="refresh"):
            worker()

    assert clone_path.exists()
    cleanup.assert_not_called()
    scheduler.acquire_write_lock.assert_not_called()


def test_remove_refuses_held_write_lock_before_cleanup(manager):
    """Removal must fail loudly when the shared write lock is unavailable."""
    alias = "locked-repo"
    clone_path = _register_repo(manager, alias)
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = False
    manager._refresh_scheduler = scheduler

    worker = _worker_for(manager, alias)
    with patch.object(manager, "_cleanup_repository_files") as cleanup:
        with pytest.raises(GitOperationError, match="write lock"):
            worker()

    assert clone_path.exists()
    cleanup.assert_not_called()
    scheduler.acquire_write_lock.assert_called_once_with(
        alias, owner_name="remove_repo"
    )


def test_failed_cleanup_is_quarantined_and_alias_can_be_readded(manager):
    """A partial deletion must not permanently block a later registration."""
    alias = "recoverable-repo"
    clone_path = _register_repo(manager, alias)
    scheduler = MagicMock()
    scheduler.acquire_write_lock.return_value = True
    manager._refresh_scheduler = scheduler

    worker = _worker_for(manager, alias)
    with patch.object(manager, "_cleanup_repository_files", return_value=False):
        with pytest.raises(GitOperationError, match="cleanup incomplete"):
            worker()

    assert not clone_path.exists()
    quarantined = list(clone_path.parent.glob(f"{alias}.corrupt-*"))
    assert len(quarantined) == 1
    scheduler.release_write_lock.assert_called_once_with(
        alias, owner_name="remove_repo"
    )

    replacement_path = clone_path

    def create_replacement(*args, **kwargs):
        replacement_path.mkdir(parents=True)
        return str(replacement_path)

    with (
        patch.object(manager, "_validate_git_repository", return_value=True),
        patch.object(manager, "_clone_repository", side_effect=create_replacement),
        patch.object(manager, "_execute_post_clone_workflow", return_value=None),
    ):
        manager.add_golden_repo(
            repo_url=f"https://example.test/{alias}.git",
            alias=alias,
            default_branch="main",
        )
        add_worker = manager.background_job_manager.submit_job.call_args.kwargs["func"]
        result = add_worker()

    assert result["success"] is True
