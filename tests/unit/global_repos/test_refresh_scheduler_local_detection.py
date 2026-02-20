"""
Unit tests for Story #224: RefreshScheduler local repo handling (C1-C3).

C1: _scheduler_loop() must NOT skip local:// repos (remove the early continue).
C2: _execute_refresh() uses mtime-based change detection for local repos.
C3: _execute_refresh() skips GitPullUpdater and uses live source path.
"""

import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_golden_repos_dir():
    """Create temporary golden repos directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_registry():
    """Create a mock registry."""
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    return registry


@pytest.fixture
def mock_config_source():
    """Create a mock config source with short interval so tests don't hang."""
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return config_source


@pytest.fixture
def scheduler(
    temp_golden_repos_dir,
    mock_registry,
    mock_config_source,
):
    """Create a RefreshScheduler with injected mock registry."""
    return RefreshScheduler(
        golden_repos_dir=temp_golden_repos_dir,
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# C1: _scheduler_loop() must NOT skip local:// repos
# ---------------------------------------------------------------------------


class TestSchedulerLoopIncludesLocalRepos:
    """C1: local:// repos must reach _submit_refresh_job in the scheduler loop."""

    def test_scheduler_loop_submits_local_repo(
        self, scheduler, mock_registry
    ):
        """
        _scheduler_loop() must call _submit_refresh_job for local:// repos.

        Previously the loop had an early 'continue' that skipped local repos.
        After C1 that skip is removed, so local repos are submitted for refresh.

        Strategy: run the real _scheduler_loop() in a thread, capture any call
        to _submit_refresh_job, then immediately stop the scheduler so the loop
        exits on the next iteration.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local://cidx-meta",
            }
        ]

        submitted = []
        submit_event = threading.Event()

        def capture_submit(alias_name):
            submitted.append(alias_name)
            # Signal that we captured a submission, then stop the scheduler
            submit_event.set()
            scheduler._running = False
            scheduler._stop_event.set()

        with patch.object(
            scheduler, "_submit_refresh_job", side_effect=capture_submit
        ):
            scheduler._running = True
            scheduler._stop_event.clear()
            t = threading.Thread(target=scheduler._scheduler_loop, daemon=True)
            t.start()
            # Wait up to 5 seconds for the submission
            submit_event.wait(timeout=5)
            scheduler._running = False
            scheduler._stop_event.set()
            t.join(timeout=3)

        assert "cidx-meta-global" in submitted, (
            "Local repo cidx-meta-global must be submitted for refresh. "
            "C1: the 'if repo_url.startswith(\"local://\"): continue' block "
            "must be removed from _scheduler_loop()."
        )

    def test_scheduler_loop_submits_git_repos(
        self, scheduler, mock_registry
    ):
        """Git repos must also be submitted (regression guard for C1)."""
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "some-repo-global",
                "repo_url": "git@github.com:org/repo.git",
            }
        ]

        submitted = []
        submit_event = threading.Event()

        def capture_submit(alias_name):
            submitted.append(alias_name)
            submit_event.set()
            scheduler._running = False
            scheduler._stop_event.set()

        with patch.object(
            scheduler, "_submit_refresh_job", side_effect=capture_submit
        ):
            scheduler._running = True
            scheduler._stop_event.clear()
            t = threading.Thread(target=scheduler._scheduler_loop, daemon=True)
            t.start()
            submit_event.wait(timeout=5)
            scheduler._running = False
            scheduler._stop_event.set()
            t.join(timeout=3)

        assert "some-repo-global" in submitted


# ---------------------------------------------------------------------------
# C2: _execute_refresh() uses _has_local_changes for local:// repos
# ---------------------------------------------------------------------------


class TestExecuteRefreshLocalRepoMtimeDetection:
    """C2: _execute_refresh() must use _has_local_changes for local repos."""

    def _setup_mocks(self, scheduler, alias_name, alias_target, repo_info):
        """Configure standard mocks for _execute_refresh tests."""
        scheduler.alias_manager.read_alias = MagicMock(return_value=alias_target)
        scheduler.registry.get_global_repo = MagicMock(return_value=repo_info)
        scheduler.registry.update_refresh_timestamp = MagicMock()
        scheduler.registry.update_enable_temporal = MagicMock()
        scheduler.registry.update_enable_scip = MagicMock()

    def test_local_repo_calls_has_local_changes(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        _execute_refresh must call _has_local_changes for local:// repos.

        C2: mtime-based detection replaces git has_changes() for local repos.
        """
        alias_name = "cidx-meta-global"
        live_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        live_dir.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "local://cidx-meta",
            "enable_temporal": False,
            "enable_scip": False,
        }
        self._setup_mocks(scheduler, alias_name, str(live_dir), repo_info)

        mtime_calls = []

        def capture_has_local_changes(source_path, alias):
            mtime_calls.append((source_path, alias))
            return False  # No changes detected

        with patch.object(
            scheduler, "_has_local_changes", side_effect=capture_has_local_changes
        ):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    scheduler._execute_refresh(alias_name)

        assert len(mtime_calls) == 1, (
            "_has_local_changes must be called exactly once for local:// repos"
        )
        _, called_alias = mtime_calls[0]
        assert called_alias == alias_name

    def test_local_repo_no_changes_returns_no_changes_message(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        When _has_local_changes returns False, refresh returns 'No changes detected'.
        """
        alias_name = "cidx-meta-global"
        live_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        live_dir.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "local://cidx-meta",
            "enable_temporal": False,
            "enable_scip": False,
        }
        self._setup_mocks(scheduler, alias_name, str(live_dir), repo_info)

        with patch.object(scheduler, "_has_local_changes", return_value=False):
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    result = scheduler._execute_refresh(alias_name)

        assert result["success"] is True
        assert "No changes" in result["message"]

    def test_git_repo_does_not_call_has_local_changes(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        _has_local_changes must NOT be called for git repos (regression guard).
        """
        alias_name = "some-repo-global"
        repo_dir = Path(temp_golden_repos_dir) / "some-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/repo.git",
            "enable_temporal": False,
            "enable_scip": False,
        }
        self._setup_mocks(scheduler, alias_name, str(repo_dir), repo_info)

        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = False

        mtime_calls = []

        with patch.object(
            scheduler, "_has_local_changes", side_effect=lambda *a: mtime_calls.append(a) or False
        ):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
                return_value=mock_updater,
            ):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(
                        scheduler, "_reconcile_registry_with_filesystem"
                    ):
                        scheduler._execute_refresh(alias_name)

        assert mtime_calls == [], (
            "_has_local_changes must NOT be called for git repos"
        )


# ---------------------------------------------------------------------------
# C3: _execute_refresh() skips GitPullUpdater, uses live source path
# ---------------------------------------------------------------------------


class TestExecuteRefreshLocalRepoSourcePath:
    """C3: _execute_refresh() must use live directory as source_path for local repos."""

    def test_local_repo_skips_git_pull_updater(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        GitPullUpdater must NOT be instantiated for local:// repos.

        C3: local repos skip git pull entirely.
        """
        alias_name = "cidx-meta-global"
        live_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        live_dir.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "local://cidx-meta",
            "enable_temporal": False,
            "enable_scip": False,
        }
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(live_dir))
        scheduler.registry.get_global_repo = MagicMock(return_value=repo_info)
        scheduler.registry.update_refresh_timestamp = MagicMock()

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater"
        ) as mock_git_cls:
            with patch.object(scheduler, "_has_local_changes", return_value=False):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(
                        scheduler, "_reconcile_registry_with_filesystem"
                    ):
                        scheduler._execute_refresh(alias_name)

        mock_git_cls.assert_not_called()

    def test_local_repo_uses_live_dir_as_source_path(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        source_path passed to _create_new_index must be the LIVE cidx-meta directory.

        C3: even when alias target points to a versioned snapshot, the source_path
        for indexing must be the live directory (where writers put new files).
        """
        alias_name = "cidx-meta-global"
        live_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        live_dir.mkdir(parents=True, exist_ok=True)

        # Alias points to versioned snapshot (NOT the live dir)
        versioned_target = (
            Path(temp_golden_repos_dir)
            / ".versioned"
            / "cidx-meta"
            / "v_1700000000"
        )
        versioned_target.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "local://cidx-meta",
            "enable_temporal": False,
            "enable_scip": False,
        }
        scheduler.alias_manager.read_alias = MagicMock(
            return_value=str(versioned_target)
        )
        scheduler.registry.get_global_repo = MagicMock(return_value=repo_info)
        scheduler.registry.update_refresh_timestamp = MagicMock()

        captured_source_paths = []

        def capture_index_source(alias_name, source_path):
            # Capture the source_path then stop — _create_snapshot never runs
            captured_source_paths.append(source_path)
            raise RuntimeError("Stop after capture")

        with patch.object(scheduler, "_has_local_changes", return_value=True):
            with patch.object(
                scheduler, "_index_source", side_effect=capture_index_source
            ):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(
                        scheduler, "_reconcile_registry_with_filesystem"
                    ):
                        with pytest.raises(RuntimeError):
                            scheduler._execute_refresh(alias_name)

        assert len(captured_source_paths) == 1
        used_path = captured_source_paths[0]
        assert used_path == str(live_dir), (
            f"Expected live source path {live_dir}, got {used_path}. "
            "C3: local repos must use live cidx-meta dir, not versioned snapshot."
        )

    def test_git_repo_still_uses_git_pull_updater(
        self, scheduler, temp_golden_repos_dir
    ):
        """
        Git repos must still use GitPullUpdater (regression guard for C3).
        """
        alias_name = "some-repo-global"
        repo_dir = Path(temp_golden_repos_dir) / "some-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        repo_info = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:org/repo.git",
            "enable_temporal": False,
            "enable_scip": False,
        }
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(repo_dir))
        scheduler.registry.get_global_repo = MagicMock(return_value=repo_info)
        scheduler.registry.update_refresh_timestamp = MagicMock()
        scheduler.registry.update_enable_temporal = MagicMock()
        scheduler.registry.update_enable_scip = MagicMock()

        mock_updater = MagicMock()
        mock_updater.has_changes.return_value = False

        with patch(
            "code_indexer.global_repos.refresh_scheduler.GitPullUpdater",
            return_value=mock_updater,
        ) as mock_git_cls:
            with patch.object(
                scheduler, "_detect_existing_indexes", return_value={}
            ):
                with patch.object(
                    scheduler, "_reconcile_registry_with_filesystem"
                ):
                    scheduler._execute_refresh(alias_name)

        mock_git_cls.assert_called_once()
