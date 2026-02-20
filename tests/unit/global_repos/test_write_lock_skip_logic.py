"""
Unit tests for RefreshScheduler write-lock skip logic and trigger method (Story #227).

Tests:
- _execute_refresh() skips CoW clone when write lock is held for local repos (AC1)
- _execute_refresh() proceeds normally when write lock is NOT held (mtime detection)
- _execute_refresh() does NOT check write lock for git repos (AC4)
- trigger_refresh_for_repo() submits job via BackgroundJobManager
- trigger_refresh_for_repo() falls back to direct execution in CLI mode

RED phase: Tests written BEFORE production code. All tests expected to FAIL
until production code is implemented.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def registry(golden_repos_dir):
    return GlobalRegistry(str(golden_repos_dir))


@pytest.fixture
def alias_manager(golden_repos_dir):
    return AliasManager(str(golden_repos_dir / "aliases"))


@pytest.fixture
def scheduler(golden_repos_dir, config_mgr, query_tracker, cleanup_manager, registry):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )


def _setup_local_repo(golden_repos_dir, alias_manager, registry, alias_name="cidx-meta-global"):
    """Helper: create local repo dir, alias, and registry entry."""
    repo_name = alias_name.replace("-global", "")
    local_repo_dir = golden_repos_dir / repo_name
    local_repo_dir.mkdir(exist_ok=True)
    alias_manager.create_alias(alias_name, str(local_repo_dir))
    registry.register_global_repo(
        repo_name,
        alias_name,
        f"local://{repo_name}",
        str(local_repo_dir),
        allow_reserved=(repo_name == "cidx-meta"),
    )
    return local_repo_dir


def _setup_git_repo(golden_repos_dir, alias_manager, registry, alias_name="test-repo-global"):
    """Helper: create git repo dir, alias, and registry entry."""
    repo_name = alias_name.replace("-global", "")
    remote_repo_dir = golden_repos_dir / repo_name
    remote_repo_dir.mkdir(exist_ok=True)
    alias_manager.create_alias(alias_name, str(remote_repo_dir))
    registry.register_global_repo(
        repo_name,
        alias_name,
        "git@github.com:org/repo.git",
        str(remote_repo_dir),
    )
    return remote_repo_dir


class TestRefreshSchedulerWriteLockSkip:
    """Tests for RefreshScheduler write-lock skip logic for local repos."""

    def test_execute_refresh_skips_local_repo_when_write_locked(
        self, scheduler, golden_repos_dir, alias_manager, registry
    ):
        """
        AC1: When write lock is held, _execute_refresh() skips CoW clone for local repos.

        Expected: {"success": True, message containing "skip" or "lock"}
        No CoW clone (_create_new_index) must be attempted.
        """
        _setup_local_repo(golden_repos_dir, alias_manager, registry)

        scheduler.acquire_write_lock("cidx-meta")

        try:
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
                 patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
                 patch.object(scheduler, "_create_new_index") as mock_create_index:

                result = scheduler._execute_refresh("cidx-meta-global")

                mock_create_index.assert_not_called()

                assert result["success"] is True, "Result must be success=True"
                message = result.get("message", "")
                assert "skip" in message.lower() or "lock" in message.lower(), (
                    f"Result message must indicate skip due to write lock. Got: '{message}'"
                )
        finally:
            scheduler.release_write_lock("cidx-meta")

    def test_execute_refresh_proceeds_local_repo_when_not_locked(
        self, scheduler, golden_repos_dir, alias_manager, registry
    ):
        """
        When write lock is NOT held, _execute_refresh() proceeds with mtime detection.

        _has_local_changes() must be called (normal execution path).
        """
        _setup_local_repo(golden_repos_dir, alias_manager, registry)

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch.object(scheduler, "_has_local_changes", return_value=False) as mock_mtime:

            result = scheduler._execute_refresh("cidx-meta-global")

            mock_mtime.assert_called_once()

            assert result["success"] is True
            assert "no changes" in result.get("message", "").lower(), (
                "When no write lock and no changes, result must indicate 'No changes detected'"
            )

    def test_execute_refresh_does_not_check_write_lock_for_git_repos(
        self, scheduler, golden_repos_dir, alias_manager, registry
    ):
        """
        AC4: For git repos, _execute_refresh() does NOT call is_write_locked().

        Git repos manage writes via git pull — write-lock is not applicable.
        """
        _setup_git_repo(golden_repos_dir, alias_manager, registry)

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch.object(scheduler, "is_write_locked") as mock_is_locked, \
             patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_cls:

            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False
            mock_cls.return_value = mock_updater

            scheduler._execute_refresh("test-repo-global")

            mock_is_locked.assert_not_called()


class TestRefreshSchedulerTrigger:
    """Tests for RefreshScheduler.trigger_refresh_for_repo() method."""

    def test_trigger_refresh_submits_job_to_background_manager(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        When background_job_manager is set, trigger_refresh_for_repo() routes via _submit_refresh_job.

        AC2: Writers call trigger after releasing lock; BackgroundJobManager handles visibility.
        """
        # Register the alias so _resolve_global_alias() can find it
        _setup_local_repo(golden_repos_dir, alias_manager, registry)

        mock_bgm = MagicMock()
        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
            background_job_manager=mock_bgm,
        )

        with patch.object(sched, "_submit_refresh_job") as mock_submit:
            sched.trigger_refresh_for_repo("cidx-meta-global")

            mock_submit.assert_called_once_with("cidx-meta-global", submitter_username="system")

    def test_trigger_refresh_falls_back_to_direct_execution(
        self,
        golden_repos_dir,
        config_mgr,
        query_tracker,
        cleanup_manager,
        registry,
        alias_manager,
    ):
        """
        When no background_job_manager (CLI mode), trigger_refresh_for_repo() calls _execute_refresh.
        """
        # Register the alias so _resolve_global_alias() can find it
        _setup_local_repo(golden_repos_dir, alias_manager, registry)

        sched = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
            background_job_manager=None,
        )

        with patch.object(sched, "_execute_refresh") as mock_execute:
            mock_execute.return_value = {"success": True}
            sched.trigger_refresh_for_repo("cidx-meta-global")

            mock_execute.assert_called_once_with("cidx-meta-global")


class TestConcurrentWriterAndRefreshScheduler:
    """Integration test for concurrent writer + refresh scheduler interaction."""

    def test_concurrent_writer_blocks_refresh_and_proceeds_after_release(
        self, scheduler, golden_repos_dir, alias_manager, registry
    ):
        """
        Full AC1 + AC2 scenario:
        1. Writer acquires lock
        2. RefreshScheduler detects lock, skips (no CoW clone)
        3. Writer releases lock
        4. RefreshScheduler runs again — mtime check proceeds normally
        """
        local_repo_dir = golden_repos_dir / "cidx-meta"
        local_repo_dir.mkdir(exist_ok=True)
        alias_manager.create_alias("cidx-meta-global", str(local_repo_dir))
        registry.register_global_repo(
            "cidx-meta",
            "cidx-meta-global",
            "local://cidx-meta",
            str(local_repo_dir),
            allow_reserved=True,
        )

        # Step 1: Acquire write lock (simulating writer)
        acquired = scheduler.acquire_write_lock("cidx-meta")
        assert acquired is True

        # Step 2: Refresh while lock is held — must skip
        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch.object(scheduler, "_create_new_index") as mock_create:

            result = scheduler._execute_refresh("cidx-meta-global")

            assert result["success"] is True
            assert not mock_create.called, "CoW clone must NOT be called while write lock is held"
            message = result.get("message", "")
            assert "skip" in message.lower() or "lock" in message.lower(), (
                f"Refresh must indicate it was skipped. Got: '{message}'"
            )

        # Step 3: Release lock (writer done)
        scheduler.release_write_lock("cidx-meta")

        # Step 4: Refresh after lock release — must proceed to mtime check
        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch.object(scheduler, "_has_local_changes", return_value=False):

            result2 = scheduler._execute_refresh("cidx-meta-global")

            assert result2["success"] is True
            assert "no changes" in result2.get("message", "").lower(), (
                "After lock release, refresh must proceed to mtime detection"
            )
