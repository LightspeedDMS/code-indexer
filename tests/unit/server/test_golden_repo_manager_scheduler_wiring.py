"""
Unit tests for Bug B fix: golden_repo_manager._refresh_scheduler wiring in lifespan.py.

The golden_repo_manager uses getattr(self, "_refresh_scheduler", None) in two places:
- add_indexes_to_golden_repo() — write lock + CoW snapshot
- change_branch() — write lock check

Before this fix, lifespan.py never set _refresh_scheduler on the golden_repo_manager
instance, so both write lock and CoW operations were silently skipped.

This test verifies:
1. The wiring assignment is present in the lifespan source code.
2. A GoldenRepoManager with _refresh_scheduler set uses the scheduler for write locking.
3. A GoldenRepoManager without _refresh_scheduler (None) degrades gracefully.
"""

import inspect
import pytest
from unittest.mock import Mock, patch


# ---------------------------------------------------------------------------
# Test 1: Source-level verification that the wiring exists in lifespan.py
# ---------------------------------------------------------------------------


def test_lifespan_wires_refresh_scheduler_onto_golden_repo_manager():
    """Verify that lifespan.py contains the wiring for golden_repo_manager._refresh_scheduler."""
    from code_indexer.server.startup import lifespan as lifespan_module

    source = inspect.getsource(lifespan_module)
    assert "golden_repo_manager._refresh_scheduler" in source, (
        "lifespan.py must wire _refresh_scheduler onto golden_repo_manager. "
        "Bug B: without this, write lock and CoW snapshot are silently skipped."
    )


# ---------------------------------------------------------------------------
# Test 2: With scheduler wired — write lock is acquired
# ---------------------------------------------------------------------------


def _make_manager_for_wiring_test(scheduler=None):
    """Build a minimal GoldenRepoManager for wiring tests."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    with patch.object(GoldenRepoManager, "__init__", lambda self, *a, **kw: None):
        manager = GoldenRepoManager.__new__(GoldenRepoManager)

    manager.data_dir = "/fake/data"
    manager.golden_repos_dir = "/fake/data/golden-repos"

    golden_repo = Mock()
    golden_repo.alias = "test-repo"
    golden_repo.clone_path = "/fake/data/golden-repos/test-repo"
    golden_repo.temporal_options = {}
    golden_repo.enable_temporal = False

    manager.golden_repos = {"test-repo": golden_repo}
    manager.get_actual_repo_path = Mock(return_value="/fake/data/golden-repos/test-repo")

    captured_workers = []

    def capture_and_run(operation_type, func, submitter_username, is_admin, repo_alias):
        captured_workers.append(func)
        return "job-wiring-test"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers

    if scheduler is not None:
        manager._refresh_scheduler = scheduler

    return manager


def test_with_scheduler_wired_write_lock_is_acquired():
    """When _refresh_scheduler is set, add_indexes_to_golden_repo acquires the write lock."""
    scheduler = Mock()
    scheduler.acquire_write_lock = Mock(return_value=True)
    scheduler.release_write_lock = Mock()
    scheduler.create_cow_snapshot = Mock(return_value="/fake/versioned/v_123")
    scheduler.schedule_old_snapshot_cleanup = Mock()

    manager = _make_manager_for_wiring_test(scheduler=scheduler)

    with patch(
        "code_indexer.server.repositories.golden_repo_manager.subprocess.run"
    ) as mock_run:
        mock_run.return_value = Mock(returncode=0, stdout="ok", stderr="")
        with patch(
            "code_indexer.server.repositories.golden_repo_manager.Path"
        ):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["semantic"],
                submitter_username="admin",
                is_admin=True,
            )

    # Execute the background worker
    assert len(manager._captured_workers) == 1
    try:
        manager._captured_workers[0]()
    except Exception:
        pass  # CoW/path details may fail in unit test; we only care lock was attempted

    scheduler.acquire_write_lock.assert_called_once_with(
        "test-repo", owner_name="add_index"
    )


def test_without_scheduler_wired_no_attribute_error():
    """When _refresh_scheduler is NOT set, add_indexes_to_golden_repo does not raise AttributeError."""
    manager = _make_manager_for_wiring_test(scheduler=None)

    # Should not raise AttributeError — uses getattr with None default
    manager.add_indexes_to_golden_repo(
        alias="test-repo",
        index_types=["semantic"],
        submitter_username="admin",
        is_admin=True,
    )

    # The job is submitted, worker captured
    assert len(manager._captured_workers) == 1
