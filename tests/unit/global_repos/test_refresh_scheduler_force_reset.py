"""
Unit tests for RefreshScheduler force_reset flag propagation (Story #272).

Tests:
1. _execute_refresh with force_reset=True skips has_changes() and calls update(force_reset=True)
2. _execute_refresh with force_reset=False (default) runs has_changes() normally
3. trigger_refresh_for_repo propagates force_reset to _submit_refresh_job
4. _submit_refresh_job captures force_reset in lambda closure for BackgroundJobManager
5. trigger_refresh_for_repo direct execution path (no BackgroundJobManager) with force_reset
"""

from unittest.mock import Mock, patch, call

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures (reuse pattern from test_refresh_scheduler_git_pull_location.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "my-repo-global",
        "repo_url": "git@github.com:org/my-repo.git",
    }
    registry.list_global_repos.return_value = []
    registry.update_refresh_timestamp.return_value = None
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    """Create RefreshScheduler with a mock registry (no BackgroundJobManager)."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


@pytest.fixture
def mock_background_job_manager():
    """Mock BackgroundJobManager for server-mode tests."""
    bjm = Mock()
    bjm.submit_job.return_value = "test-job-id-123"
    return bjm


@pytest.fixture
def scheduler_with_bjm(
    golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
    mock_background_job_manager,
):
    """Create RefreshScheduler with a BackgroundJobManager (server mode)."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
        background_job_manager=mock_background_job_manager,
    )


# ---------------------------------------------------------------------------
# AC4: _execute_refresh with force_reset=True skips has_changes()
# ---------------------------------------------------------------------------


class TestExecuteRefreshForceReset:
    """
    AC4: _execute_refresh() with force_reset=True must NOT call has_changes(),
    and must call updater.update(force_reset=True).
    """

    def test_force_reset_skips_has_changes(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When force_reset=True, has_changes() must NOT be called.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=master_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
        ):
            scheduler._execute_refresh(alias_name, force_reset=True)

        # has_changes() must NOT have been called
        mock_updater.has_changes.assert_not_called()

    def test_force_reset_calls_update_with_force_reset_true(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When force_reset=True, updater.update() must be called with force_reset=True.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        mock_updater = Mock()
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=master_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
        ):
            scheduler._execute_refresh(alias_name, force_reset=True)

        # update() must have been called with force_reset=True
        mock_updater.update.assert_called_once_with(force_reset=True)

    def test_force_reset_false_calls_has_changes_normally(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When force_reset=False (default), has_changes() IS called normally.
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        mock_updater = Mock()
        mock_updater.has_changes.return_value = True
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=master_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
        ):
            scheduler._execute_refresh(alias_name)  # No force_reset

        # has_changes() must have been called
        mock_updater.has_changes.assert_called_once()

    def test_force_reset_skips_early_return_when_no_changes(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When force_reset=True, the refresh must proceed even if has_changes() would
        return False (we skip it, so no early return).
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        mock_updater = Mock()
        # Even if has_changes would return False, force_reset bypasses it
        mock_updater.has_changes.return_value = False
        mock_updater.get_source_path.return_value = master_path

        mock_index_source = Mock()
        mock_create_snapshot = Mock(return_value=new_versioned_path)

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=master_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source", mock_index_source),
            patch.object(scheduler, "_create_snapshot", mock_create_snapshot),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
        ):
            result = scheduler._execute_refresh(alias_name, force_reset=True)

        # Indexing must have proceeded despite has_changes not being called
        mock_index_source.assert_called_once()
        mock_create_snapshot.assert_called_once()

    def test_force_reset_logs_skip_change_detection(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When force_reset=True, a log message must indicate change detection is skipped.
        """
        import logging

        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")
        new_versioned_path = str(
            golden_repos_dir / ".versioned" / "my-repo" / "v_2000000"
        )

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        mock_updater = Mock()
        mock_updater.get_source_path.return_value = master_path

        with (
            patch.object(
                scheduler.alias_manager, "read_alias", return_value=master_path
            ),
            patch.object(scheduler.alias_manager, "swap_alias"),
            patch.object(scheduler, "_detect_existing_indexes", return_value={}),
            patch.object(scheduler, "_reconcile_registry_with_filesystem"),
            patch.object(scheduler, "_index_source"),
            patch.object(scheduler, "_create_snapshot", return_value=new_versioned_path),
            patch.object(scheduler.cleanup_manager, "schedule_cleanup"),
            patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ),
        ):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.logger"
            ) as mock_logger:
                scheduler._execute_refresh(alias_name, force_reset=True)

        # Some info log about force_reset or skipping change detection should appear
        info_calls = [str(c) for c in mock_logger.info.call_args_list]
        force_reset_logged = any(
            "force" in msg.lower() or "reset" in msg.lower() or "skip" in msg.lower()
            for msg in info_calls
        )
        assert force_reset_logged, (
            f"Expected log message about force_reset or skipping change detection. "
            f"Got: {info_calls}"
        )


# ---------------------------------------------------------------------------
# AC3: trigger_refresh_for_repo propagates force_reset
# ---------------------------------------------------------------------------


class TestTriggerRefreshForRepoForceReset:
    """
    AC3: trigger_refresh_for_repo() must accept and propagate force_reset parameter.
    """

    def test_trigger_refresh_propagates_force_reset_to_submit_job(
        self, scheduler_with_bjm, mock_registry, mock_background_job_manager
    ):
        """
        trigger_refresh_for_repo(force_reset=True) must pass force_reset to
        _submit_refresh_job.
        """
        mock_registry.get_global_repo.return_value = {
            "alias_name": "my-repo-global",
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with patch.object(
            scheduler_with_bjm, "_submit_refresh_job", return_value="job-id"
        ) as mock_submit:
            scheduler_with_bjm.trigger_refresh_for_repo(
                "my-repo-global",
                submitter_username="admin",
                force_reset=True,
            )

        mock_submit.assert_called_once_with(
            "my-repo-global",
            submitter_username="admin",
            force_reset=True,
        )

    def test_trigger_refresh_propagates_force_reset_false_by_default(
        self, scheduler_with_bjm, mock_registry
    ):
        """
        trigger_refresh_for_repo() without force_reset must default to False.
        """
        mock_registry.get_global_repo.return_value = {
            "alias_name": "my-repo-global",
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with patch.object(
            scheduler_with_bjm, "_submit_refresh_job", return_value="job-id"
        ) as mock_submit:
            scheduler_with_bjm.trigger_refresh_for_repo(
                "my-repo-global",
                submitter_username="admin",
            )

        # force_reset should NOT be True
        call_kwargs = mock_submit.call_args[1] if mock_submit.call_args[1] else {}
        call_args = mock_submit.call_args[0]
        force_reset_value = call_kwargs.get("force_reset", False)
        assert force_reset_value is False

    def test_trigger_refresh_direct_execution_with_force_reset(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        When no BackgroundJobManager, trigger_refresh_for_repo(force_reset=True)
        must call _execute_refresh(alias, force_reset=True) directly.
        """
        mock_registry.get_global_repo.return_value = {
            "alias_name": "my-repo-global",
            "repo_url": "git@github.com:org/my-repo.git",
        }

        with patch.object(
            scheduler, "_execute_refresh", return_value={"success": True}
        ) as mock_execute:
            scheduler.trigger_refresh_for_repo(
                "my-repo-global",
                submitter_username="admin",
                force_reset=True,
            )

        mock_execute.assert_called_once_with("my-repo-global", force_reset=True)


# ---------------------------------------------------------------------------
# AC3: _submit_refresh_job captures force_reset in lambda closure
# ---------------------------------------------------------------------------


class TestSubmitRefreshJobForceReset:
    """
    AC3: _submit_refresh_job() must capture force_reset in the lambda closure
    so BackgroundJobManager executes the refresh with the correct flag.
    """

    def test_submit_job_passes_force_reset_true_in_lambda(
        self,
        scheduler_with_bjm,
        golden_repos_dir,
        mock_registry,
        mock_background_job_manager,
    ):
        """
        _submit_refresh_job(force_reset=True) must submit a lambda that calls
        _execute_refresh(alias, force_reset=True).
        """
        alias_name = "my-repo-global"
        master_path = str(golden_repos_dir / "my-repo")

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        # Capture the lambda submitted to BackgroundJobManager
        captured_funcs = []

        def capture_submit_job(**kwargs):
            captured_funcs.append(kwargs.get("func"))
            return "job-123"

        mock_background_job_manager.submit_job.side_effect = capture_submit_job

        scheduler_with_bjm._submit_refresh_job(
            alias_name, submitter_username="admin", force_reset=True
        )

        assert len(captured_funcs) == 1, "Expected exactly one job submission"

        # Now invoke the captured lambda and verify it calls _execute_refresh correctly
        with patch.object(
            scheduler_with_bjm, "_execute_refresh", return_value={"success": True}
        ) as mock_execute:
            captured_funcs[0]()

        mock_execute.assert_called_once_with(alias_name, force_reset=True)

    def test_submit_job_passes_force_reset_false_in_lambda(
        self,
        scheduler_with_bjm,
        golden_repos_dir,
        mock_registry,
        mock_background_job_manager,
    ):
        """
        _submit_refresh_job(force_reset=False) must submit a lambda that calls
        _execute_refresh(alias, force_reset=False).
        """
        alias_name = "my-repo-global"

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/my-repo.git",
        }

        captured_funcs = []

        def capture_submit_job(**kwargs):
            captured_funcs.append(kwargs.get("func"))
            return "job-456"

        mock_background_job_manager.submit_job.side_effect = capture_submit_job

        scheduler_with_bjm._submit_refresh_job(
            alias_name, submitter_username="admin", force_reset=False
        )

        assert len(captured_funcs) == 1

        with patch.object(
            scheduler_with_bjm, "_execute_refresh", return_value={"success": True}
        ) as mock_execute:
            captured_funcs[0]()

        mock_execute.assert_called_once_with(alias_name, force_reset=False)

    def test_submit_job_default_force_reset_is_false(
        self,
        scheduler_with_bjm,
        golden_repos_dir,
        mock_registry,
        mock_background_job_manager,
    ):
        """
        _submit_refresh_job() without force_reset must default to False.
        """
        alias_name = "my-repo-global"

        (golden_repos_dir / "my-repo").mkdir(parents=True, exist_ok=True)

        captured_funcs = []

        def capture_submit_job(**kwargs):
            captured_funcs.append(kwargs.get("func"))
            return "job-789"

        mock_background_job_manager.submit_job.side_effect = capture_submit_job

        # Call without force_reset argument
        scheduler_with_bjm._submit_refresh_job(alias_name)

        assert len(captured_funcs) == 1

        with patch.object(
            scheduler_with_bjm, "_execute_refresh", return_value={"success": True}
        ) as mock_execute:
            captured_funcs[0]()

        # Default must be force_reset=False
        mock_execute.assert_called_once_with(alias_name, force_reset=False)
