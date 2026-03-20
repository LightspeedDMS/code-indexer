"""
Unit tests for Bug #473: Golden Repo Index Rebuild — Missing Write Lock and CoW Snapshot.

Two bugs fixed:
1. add_index_to_golden_repo() did NOT acquire the write lock before indexing.
2. After indexing completes, NO CoW snapshot was created — rebuilt indexes
   are invisible because queries are served from the versioned snapshot.

The new add_indexes_to_golden_repo() method (plural) fixes both:
- Acquires write lock before indexing starts
- Releases write lock in finally block (even on failure)
- Creates CoW snapshot + performs alias swap after all index types succeed
- Schedules cleanup of old versioned snapshot (if applicable)
- Raises GoldenRepoError on write lock contention
- Handles multiple index types in a single atomic background job
- Gracefully degrades when _refresh_scheduler is None (test environments)

The old add_index_to_golden_repo() (singular) is kept for backward compatibility
and delegates to add_indexes_to_golden_repo().
"""

from unittest.mock import Mock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(scheduler=None):
    """
    Instantiate a GoldenRepoManager bypassing __init__ and configure the
    minimum attributes needed to invoke add_indexes_to_golden_repo.

    Args:
        scheduler: Optional mock RefreshScheduler to attach as _refresh_scheduler.
                   Pass None to simulate test environments without a scheduler.
    """
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoError,
        GoldenRepoManager,
    )

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
    manager.get_actual_repo_path = Mock(
        return_value="/fake/data/golden-repos/test-repo"
    )

    # background_job_manager: execute submitted func synchronously so tests
    # can observe the background_worker's effects without threading.
    captured_workers = []

    def capture_and_run(operation_type, func, submitter_username, is_admin, repo_alias):
        captured_workers.append(func)
        return "job-473"

    manager.background_job_manager = Mock()
    manager.background_job_manager.submit_job.side_effect = capture_and_run
    manager._captured_workers = captured_workers

    if scheduler is not None:
        manager._refresh_scheduler = scheduler

    return manager, GoldenRepoError


def _make_scheduler(
    lock_acquired=True, current_target="/fake/.versioned/test-repo/v_100"
):
    """Create a minimal mock RefreshScheduler."""
    scheduler = Mock()
    scheduler.acquire_write_lock = Mock(return_value=lock_acquired)
    scheduler.release_write_lock = Mock()

    alias_manager = Mock()
    alias_manager.read_alias = Mock(return_value=current_target)
    alias_manager.swap_alias = Mock()
    scheduler.alias_manager = alias_manager

    cleanup_manager = Mock()
    cleanup_manager.schedule_cleanup = Mock()
    scheduler.cleanup_manager = cleanup_manager

    scheduler._create_snapshot = Mock(return_value="/fake/.versioned/test-repo/v_999")

    return scheduler


def _run_worker(manager):
    """Execute the background worker that was captured during submit_job."""
    assert manager._captured_workers, "No background worker was submitted"
    worker = manager._captured_workers[-1]
    return worker()


def _make_ok_subprocess_result():
    result = Mock()
    result.returncode = 0
    result.stdout = "ok"
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# Tests: write lock acquisition
# ---------------------------------------------------------------------------


class TestWriteLockAcquisition:
    """Write lock must be acquired before any indexing starts."""

    def test_write_lock_acquired_before_indexing(self):
        """acquire_write_lock is called before cidx subprocess runs."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler.acquire_write_lock.assert_called_once_with(
            "test-repo", owner_name="add_index"
        )

    def test_write_lock_acquired_for_multiple_index_types(self):
        """A single write lock is acquired even for multiple index types."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["semantic", "fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        # Only one acquire_write_lock call for both index types
        assert scheduler.acquire_write_lock.call_count == 1


# ---------------------------------------------------------------------------
# Tests: write lock release
# ---------------------------------------------------------------------------


class TestWriteLockRelease:
    """Write lock must be released in a finally block — even on failure."""

    def test_write_lock_released_after_success(self):
        """release_write_lock is called after successful indexing."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler.release_write_lock.assert_called_once_with(
            "test-repo", owner_name="add_index"
        )

    def test_write_lock_released_after_indexing_failure(self):
        """release_write_lock is called even when cidx subprocess fails."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoError

        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        fail_result = Mock()
        fail_result.returncode = 1
        fail_result.stdout = ""
        fail_result.stderr = "cidx failed"

        ok_result = _make_ok_subprocess_result()  # for cidx init

        with patch("subprocess.run", side_effect=[ok_result, fail_result]):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            with pytest.raises(GoldenRepoError):
                _run_worker(manager)

        scheduler.release_write_lock.assert_called_once_with(
            "test-repo", owner_name="add_index"
        )


# ---------------------------------------------------------------------------
# Tests: CoW snapshot creation
# ---------------------------------------------------------------------------


class TestCoWSnapshot:
    """A CoW snapshot must be created after successful indexing."""

    def test_cow_snapshot_created_after_success(self):
        """_create_snapshot is called on the base clone path after indexing."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler._create_snapshot.assert_called_once_with(
            alias_name="test-repo-global",
            source_path="/fake/data/golden-repos/test-repo",
        )

    def test_cow_snapshot_not_created_on_failure(self):
        """_create_snapshot is NOT called when cidx subprocess fails."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoError

        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        fail_result = Mock()
        fail_result.returncode = 1
        fail_result.stdout = ""
        fail_result.stderr = "cidx failed"
        ok_result = _make_ok_subprocess_result()

        with patch("subprocess.run", side_effect=[ok_result, fail_result]):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            with pytest.raises(GoldenRepoError):
                _run_worker(manager)

        scheduler._create_snapshot.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: alias swap
# ---------------------------------------------------------------------------


class TestAliasSwap:
    """Alias must be swapped to new snapshot after CoW creation."""

    def test_alias_swap_happens_after_cow(self):
        """swap_alias is called with old and new snapshot paths."""
        scheduler = _make_scheduler(
            lock_acquired=True,
            current_target="/fake/.versioned/test-repo/v_100",
        )
        scheduler._create_snapshot.return_value = "/fake/.versioned/test-repo/v_999"
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler.alias_manager.swap_alias.assert_called_once_with(
            alias_name="test-repo-global",
            new_target="/fake/.versioned/test-repo/v_999",
            old_target="/fake/.versioned/test-repo/v_100",
        )


# ---------------------------------------------------------------------------
# Tests: old snapshot cleanup
# ---------------------------------------------------------------------------


class TestOldSnapshotCleanup:
    """Old versioned snapshot must be scheduled for cleanup after swap."""

    def test_cleanup_scheduled_for_versioned_old_target(self):
        """schedule_cleanup is called with the old versioned target path."""
        scheduler = _make_scheduler(
            lock_acquired=True,
            current_target="/fake/.versioned/test-repo/v_100",
        )
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler.cleanup_manager.schedule_cleanup.assert_called_once_with(
            "/fake/.versioned/test-repo/v_100"
        )

    def test_cleanup_not_scheduled_for_master_clone(self):
        """schedule_cleanup is NOT called when old target is the master clone (not versioned)."""
        scheduler = _make_scheduler(
            lock_acquired=True,
            current_target="/fake/data/golden-repos/test-repo",  # no .versioned
        )
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        scheduler.cleanup_manager.schedule_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: write lock contention
# ---------------------------------------------------------------------------


class TestWriteLockContention:
    """If write lock cannot be acquired, raise GoldenRepoError immediately."""

    def test_contention_raises_golden_repo_error(self):
        """When acquire_write_lock returns False, GoldenRepoError is raised."""
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoError

        scheduler = _make_scheduler(lock_acquired=False)
        manager, _ = _make_manager(scheduler=scheduler)

        manager.add_indexes_to_golden_repo(
            alias="test-repo",
            index_types=["fts"],
            submitter_username="admin",
        )
        with pytest.raises(GoldenRepoError, match="refreshed or indexed"):
            _run_worker(manager)

    def test_contention_does_not_call_indexing(self):
        """When lock is contended, no subprocess runs."""
        scheduler = _make_scheduler(lock_acquired=False)
        manager, _ = _make_manager(scheduler=scheduler)

        manager.add_indexes_to_golden_repo(
            alias="test-repo",
            index_types=["fts"],
            submitter_username="admin",
        )

        with patch("subprocess.run") as mock_run:
            from code_indexer.server.repositories.golden_repo_manager import (
                GoldenRepoError,
            )

            with pytest.raises(GoldenRepoError):
                _run_worker(manager)
            mock_run.assert_not_called()

    def test_contention_does_not_release_lock(self):
        """When lock could not be acquired, release_write_lock is NOT called."""
        scheduler = _make_scheduler(lock_acquired=False)
        manager, _ = _make_manager(scheduler=scheduler)

        manager.add_indexes_to_golden_repo(
            alias="test-repo",
            index_types=["fts"],
            submitter_username="admin",
        )

        from code_indexer.server.repositories.golden_repo_manager import GoldenRepoError

        with pytest.raises(GoldenRepoError):
            _run_worker(manager)

        scheduler.release_write_lock.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: backward compatibility — single index type
# ---------------------------------------------------------------------------


class TestBackwardCompatibilitySingleIndex:
    """add_index_to_golden_repo (singular) must still work correctly."""

    def test_singular_method_delegates_to_plural(self):
        """add_index_to_golden_repo calls add_indexes_to_golden_repo with list."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch.object(
            manager, "add_indexes_to_golden_repo", return_value="job-473"
        ) as mock_plural:
            manager.add_index_to_golden_repo(
                alias="test-repo",
                index_type="semantic",
                submitter_username="tester",
            )

        mock_plural.assert_called_once_with(
            alias="test-repo",
            index_types=["semantic"],
            submitter_username="tester",
        )

    def test_singular_method_returns_job_id(self):
        """add_index_to_golden_repo returns a job_id string."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        job_id = manager.add_index_to_golden_repo(
            alias="test-repo",
            index_type="fts",
            submitter_username="admin",
        )
        assert isinstance(job_id, str)
        assert len(job_id) > 0


# ---------------------------------------------------------------------------
# Tests: multiple index types in single job
# ---------------------------------------------------------------------------


class TestMultipleIndexTypes:
    """Multiple index types must be processed in a single background job."""

    def test_single_job_submitted_for_multiple_types(self):
        """Only one background job is submitted even with multiple index types."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        manager.add_indexes_to_golden_repo(
            alias="test-repo",
            index_types=["semantic", "fts"],
            submitter_username="admin",
        )

        assert manager.background_job_manager.submit_job.call_count == 1

    def test_all_index_types_run_in_single_worker(self):
        """Both cidx index commands run inside the same background worker."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        call_log = []

        def track_subprocess(cmd, **kwargs):
            call_log.append(cmd)
            return _make_ok_subprocess_result()

        with patch("subprocess.run", side_effect=track_subprocess):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["semantic", "fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        # Expect: [cidx init, cidx index --clear, cidx index --rebuild-fts-index]
        commands = [" ".join(c) for c in call_log]
        assert any(
            "--clear" in c for c in commands
        ), "Expected cidx index --clear for semantic"
        assert any(
            "--rebuild-fts-index" in c for c in commands
        ), "Expected cidx index --rebuild-fts-index for fts"

    def test_cow_snapshot_created_once_for_multiple_types(self):
        """Only one CoW snapshot is created after all index types complete."""
        scheduler = _make_scheduler(lock_acquired=True)
        manager, _ = _make_manager(scheduler=scheduler)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["semantic", "fts"],
                submitter_username="admin",
            )
            _run_worker(manager)

        assert scheduler._create_snapshot.call_count == 1


# ---------------------------------------------------------------------------
# Tests: graceful degradation without scheduler
# ---------------------------------------------------------------------------


class TestGracefulDegradationNoScheduler:
    """When _refresh_scheduler is None, skip lock and CoW gracefully."""

    def test_no_scheduler_does_not_raise(self):
        """Without a scheduler, add_indexes_to_golden_repo completes without error."""
        manager, _ = _make_manager(scheduler=None)  # No scheduler

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["fts"],
                submitter_username="admin",
            )
            result = _run_worker(manager)

        assert result["success"] is True

    def test_no_scheduler_returns_result(self):
        """Without a scheduler, background worker returns success dict."""
        manager, _ = _make_manager(scheduler=None)

        with patch("subprocess.run", return_value=_make_ok_subprocess_result()):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["semantic"],
                submitter_username="admin",
            )
            result = _run_worker(manager)

        assert "alias" in result
        assert result["alias"] == "test-repo"

    def test_no_scheduler_invalid_alias_raises_value_error(self):
        """Without a scheduler, invalid alias still raises ValueError."""
        manager, _ = _make_manager(scheduler=None)

        with pytest.raises(ValueError, match="not found"):
            manager.add_indexes_to_golden_repo(
                alias="nonexistent",
                index_types=["fts"],
                submitter_username="admin",
            )

    def test_no_scheduler_invalid_index_type_raises_value_error(self):
        """Without a scheduler, invalid index_type still raises ValueError."""
        manager, _ = _make_manager(scheduler=None)

        with pytest.raises(ValueError, match="[Ii]nvalid"):
            manager.add_indexes_to_golden_repo(
                alias="test-repo",
                index_types=["bogus"],
                submitter_username="admin",
            )
