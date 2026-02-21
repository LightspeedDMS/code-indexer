"""
Unit tests for Bug #239: Reconciliation/scheduler startup race condition.

Tests verify the two fixes:

Fix 1: reconcile_golden_repos() acquires a write lock around _restore_master_from_versioned()
       so the RefreshScheduler cannot snapshot a partially-restored master.

Fix 2: _execute_refresh() checks the write lock for GIT repos (not just local repos),
       and skips the refresh if the lock is held (protects against reconciliation race).

TDD RED phase: Tests written BEFORE production code changes. All expected to FAIL
until production code is implemented.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, call, MagicMock

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


# ---------------------------------------------------------------------------
# Fixtures
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
    """Create RefreshScheduler with mock registry for reconciliation tests."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def real_registry(golden_repos_dir):
    return GlobalRegistry(str(golden_repos_dir))


@pytest.fixture
def alias_manager(golden_repos_dir):
    return AliasManager(str(golden_repos_dir / "aliases"))


@pytest.fixture
def scheduler_with_real_registry(
    golden_repos_dir,
    config_mgr,
    mock_query_tracker,
    mock_cleanup_manager,
    real_registry,
):
    """Create RefreshScheduler with a real registry for _execute_refresh tests."""
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=real_registry,
    )


def _make_subprocess_mock(cp_calls_list=None):
    """Return a mock subprocess.run that records cp calls and succeeds."""
    def mock_subprocess_run(cmd, **kwargs):
        if cp_calls_list is not None and isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "cp":
            cp_calls_list.append(cmd)
        result = Mock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result
    return mock_subprocess_run


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


# ---------------------------------------------------------------------------
# Fix 1: reconcile_golden_repos() acquires write lock during restoration
# ---------------------------------------------------------------------------


class TestReconcileAcquiresWriteLock:
    """
    Fix 1: reconcile_golden_repos() must hold the write lock while calling
    _restore_master_from_versioned() to prevent the RefreshScheduler timer
    from creating a CoW snapshot of a partially-restored master directory.
    """

    def test_reconcile_acquires_write_lock_before_restore(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1: acquire_write_lock() must be called BEFORE _restore_master_from_versioned()
        for each repo that needs restoration.

        The lock alias is the repo_name (alias without -global suffix).
        """
        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        lock_calls: list = []

        original_acquire = scheduler.acquire_write_lock

        def tracking_acquire(alias, owner_name="reconciliation"):
            lock_calls.append(("acquire", alias, owner_name))
            return original_acquire(alias, owner_name=owner_name)

        with patch.object(scheduler, "acquire_write_lock", side_effect=tracking_acquire), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        assert any(
            op == "acquire" and "my-repo" in alias
            for op, alias, _ in lock_calls
        ), (
            f"acquire_write_lock must be called with 'my-repo' before restore. "
            f"Got calls: {lock_calls}"
        )

    def test_reconcile_releases_write_lock_after_restore(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1: release_write_lock() must be called AFTER restoration completes,
        so the RefreshScheduler can proceed with normal refresh cycles.
        """
        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        lock_calls: list = []

        original_acquire = scheduler.acquire_write_lock
        original_release = scheduler.release_write_lock

        def tracking_acquire(alias, owner_name="reconciliation"):
            lock_calls.append(("acquire", alias, owner_name))
            return original_acquire(alias, owner_name=owner_name)

        def tracking_release(alias, owner_name="reconciliation"):
            lock_calls.append(("release", alias, owner_name))
            return original_release(alias, owner_name=owner_name)

        with patch.object(scheduler, "acquire_write_lock", side_effect=tracking_acquire), \
             patch.object(scheduler, "release_write_lock", side_effect=tracking_release), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        acquire_calls = [(op, alias) for op, alias, _ in lock_calls if op == "acquire"]
        release_calls = [(op, alias) for op, alias, _ in lock_calls if op == "release"]

        assert len(acquire_calls) >= 1, (
            f"Expected at least 1 acquire call, got: {lock_calls}"
        )
        assert len(release_calls) >= 1, (
            f"Expected at least 1 release call after acquire, got: {lock_calls}"
        )

        # Verify acquire precedes release for the same alias
        for acq_op, acq_alias in acquire_calls:
            matching_releases = [
                (op, alias) for op, alias in release_calls
                if alias == acq_alias
            ]
            assert len(matching_releases) >= 1, (
                f"Lock acquired for {acq_alias} but never released. Calls: {lock_calls}"
            )

    def test_reconcile_releases_lock_even_on_restore_exception(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1 (exception safety): Write lock must be released even if
        _restore_master_from_versioned() raises an unexpected exception.
        Failing to release the lock would permanently block refresh cycles.
        """
        (golden_repos_dir / ".versioned" / "fail-repo" / "v_9000000").mkdir(
            parents=True
        )

        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "fail-repo-global",
                "repo_url": "git@github.com:org/fail-repo.git",
            },
        ]

        release_called = []

        original_release = scheduler.release_write_lock

        def tracking_release(alias, owner_name="reconciliation"):
            release_called.append(alias)
            return original_release(alias, owner_name=owner_name)

        def failing_restore(alias_name, master_path):
            raise RuntimeError("Simulated disk failure during cp")

        with patch.object(scheduler, "release_write_lock", side_effect=tracking_release), \
             patch.object(scheduler, "_restore_master_from_versioned", side_effect=failing_restore):
            # Must not propagate the exception (AC7 non-blocking)
            scheduler.reconcile_golden_repos()

        assert len(release_called) >= 1, (
            f"release_write_lock must be called even when _restore_master_from_versioned raises. "
            f"release_called={release_called}"
        )

    def test_reconcile_write_lock_acquired_with_reconciliation_owner(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1: The write lock must be acquired with owner_name='reconciliation'
        so that log messages and lock files correctly identify the holder.
        """
        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        acquire_owner_names: list = []

        original_acquire = scheduler.acquire_write_lock

        def tracking_acquire(alias, owner_name="reconciliation"):
            acquire_owner_names.append(owner_name)
            return original_acquire(alias, owner_name=owner_name)

        with patch.object(scheduler, "acquire_write_lock", side_effect=tracking_acquire), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        assert any(name == "reconciliation" for name in acquire_owner_names), (
            f"acquire_write_lock must be called with owner_name='reconciliation'. "
            f"Got owner names: {acquire_owner_names}"
        )

    def test_reconcile_does_not_acquire_lock_for_repos_with_existing_master(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1: Write lock must NOT be acquired for repos whose master dir already exists.
        Only repos that need restoration require the lock.
        """
        # Master already exists — no restoration needed
        (golden_repos_dir / "my-repo").mkdir(parents=True)

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        lock_calls: list = []

        original_acquire = scheduler.acquire_write_lock

        def tracking_acquire(alias, owner_name="reconciliation"):
            lock_calls.append(("acquire", alias, owner_name))
            return original_acquire(alias, owner_name=owner_name)

        with patch.object(scheduler, "acquire_write_lock", side_effect=tracking_acquire), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        assert len(lock_calls) == 0, (
            f"acquire_write_lock must NOT be called for repos with existing master. "
            f"Got calls: {lock_calls}"
        )

    def test_reconcile_does_not_acquire_lock_for_local_repos(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        """
        Fix 1: Local repos (local://) are skipped entirely — no lock acquisition.
        """
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "cidx-meta-global",
                "repo_url": "local:///path/to/cidx-meta",
            },
        ]

        lock_calls: list = []

        original_acquire = scheduler.acquire_write_lock

        def tracking_acquire(alias, owner_name="reconciliation"):
            lock_calls.append(("acquire", alias, owner_name))
            return original_acquire(alias, owner_name=owner_name)

        with patch.object(scheduler, "acquire_write_lock", side_effect=tracking_acquire), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        assert len(lock_calls) == 0, (
            f"acquire_write_lock must NOT be called for local repos. "
            f"Got calls: {lock_calls}"
        )

    def test_reconcile_write_lock_blocks_concurrent_refresh_during_restore(
        self, scheduler, golden_repos_dir, mock_registry, alias_manager, real_registry
    ):
        """
        Fix 1 (integration): While reconciliation holds the write lock,
        is_write_locked() must return True for that repo alias.
        This validates that the RefreshScheduler's write lock check (Fix 2)
        would correctly block a concurrent refresh during restoration.
        """
        (golden_repos_dir / ".versioned" / "my-repo" / "v_9000000").mkdir(
            parents=True
        )

        mock_registry.list_global_repos.return_value = [
            {"alias_name": "my-repo-global", "repo_url": "git@github.com:org/my-repo.git"},
        ]

        # Track whether lock was visible to is_write_locked during restore
        lock_visible_during_restore: list = []

        original_restore = scheduler._restore_master_from_versioned

        def checking_restore(alias_name, master_path):
            # At this point, reconciliation should have acquired the lock
            repo_name = alias_name.replace("-global", "")
            is_locked = scheduler.is_write_locked(repo_name)
            lock_visible_during_restore.append(is_locked)
            # Simulate the restore by creating the master_path directory
            master_path.mkdir(parents=True, exist_ok=True)
            return True

        with patch.object(scheduler, "_restore_master_from_versioned", side_effect=checking_restore), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            scheduler.reconcile_golden_repos()

        assert len(lock_visible_during_restore) >= 1, (
            "checking_restore must have been called at least once"
        )
        assert all(lock_visible_during_restore), (
            f"Write lock must be visible (True) during _restore_master_from_versioned. "
            f"Got: {lock_visible_during_restore}"
        )


# ---------------------------------------------------------------------------
# Fix 2: _execute_refresh() checks write lock for git repos
# ---------------------------------------------------------------------------


class TestExecuteRefreshGitRepoWriteLockCheck:
    """
    Fix 2: _execute_refresh() must check the write lock for GIT repos
    (not just local repos). If the lock is held (e.g. by reconciliation),
    the refresh must be skipped to avoid snapshotting a partial master.
    """

    def test_execute_refresh_skips_git_repo_when_write_locked(
        self,
        scheduler_with_real_registry,
        golden_repos_dir,
        alias_manager,
        real_registry,
    ):
        """
        Fix 2: When write lock is held for a git repo, _execute_refresh()
        must return success=True with a message indicating skip/lock,
        WITHOUT calling GitPullUpdater or creating any snapshot.
        """
        _setup_git_repo(golden_repos_dir, alias_manager, real_registry)

        scheduler = scheduler_with_real_registry

        # Acquire the write lock for this git repo (simulating reconciliation)
        acquired = scheduler.acquire_write_lock("test-repo", owner_name="reconciliation")
        assert acquired is True, "Must be able to acquire write lock for test"

        try:
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
                 patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
                 patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_git_cls, \
                 patch.object(scheduler, "_create_new_index") as mock_create_index:

                result = scheduler._execute_refresh("test-repo-global")

                # GitPullUpdater must NOT be instantiated
                mock_git_cls.assert_not_called()
                # No snapshot/index creation
                mock_create_index.assert_not_called()

                assert result["success"] is True, (
                    f"Result must be success=True when skipping locked git repo. Got: {result}"
                )
                message = result.get("message", "")
                assert "skip" in message.lower() or "lock" in message.lower(), (
                    f"Result message must indicate skip due to write lock. Got: '{message}'"
                )
        finally:
            scheduler.release_write_lock("test-repo", owner_name="reconciliation")

    def test_execute_refresh_proceeds_git_repo_when_not_locked(
        self,
        scheduler_with_real_registry,
        golden_repos_dir,
        alias_manager,
        real_registry,
    ):
        """
        Fix 2: When write lock is NOT held, _execute_refresh() must proceed normally
        with GitPullUpdater (the existing behavior must be preserved).
        """
        _setup_git_repo(golden_repos_dir, alias_manager, real_registry)

        scheduler = scheduler_with_real_registry

        # Ensure no lock is held
        assert not scheduler.is_write_locked("test-repo"), (
            "Write lock must NOT be held before this test"
        )

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_cls:

            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False
            mock_cls.return_value = mock_updater

            result = scheduler._execute_refresh("test-repo-global")

            # GitPullUpdater must be instantiated (normal flow)
            mock_cls.assert_called_once()
            assert result["success"] is True

    def test_execute_refresh_git_repo_calls_is_write_locked(
        self,
        scheduler_with_real_registry,
        golden_repos_dir,
        alias_manager,
        real_registry,
    ):
        """
        Fix 2: _execute_refresh() must call is_write_locked() for git repos.
        This is the opposite of the pre-Bug #239 behavior (where it was not called).
        """
        _setup_git_repo(golden_repos_dir, alias_manager, real_registry)

        scheduler = scheduler_with_real_registry

        with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
             patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
             patch.object(scheduler, "is_write_locked", return_value=False) as mock_is_locked, \
             patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_cls:

            mock_updater = MagicMock()
            mock_updater.has_changes.return_value = False
            mock_cls.return_value = mock_updater

            scheduler._execute_refresh("test-repo-global")

            # is_write_locked MUST be called for git repos after Fix 2
            mock_is_locked.assert_called()

    def test_execute_refresh_git_repo_write_lock_skip_preserves_success_true(
        self,
        scheduler_with_real_registry,
        golden_repos_dir,
        alias_manager,
        real_registry,
    ):
        """
        Fix 2: The skip result for write-locked git repos must be success=True
        (not an error), so BackgroundJobManager reports it as a non-failure.
        """
        _setup_git_repo(golden_repos_dir, alias_manager, real_registry)

        scheduler = scheduler_with_real_registry

        acquired = scheduler.acquire_write_lock("test-repo", owner_name="reconciliation")
        assert acquired is True

        try:
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}), \
                 patch.object(scheduler, "_reconcile_registry_with_filesystem"), \
                 patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater"):

                result = scheduler._execute_refresh("test-repo-global")

                assert result.get("success") is True, (
                    f"Skipped refresh must return success=True, got: {result}"
                )
                assert result.get("alias") == "test-repo-global" or \
                       "test-repo" in str(result), (
                    f"Result must include alias info: {result}"
                )
        finally:
            scheduler.release_write_lock("test-repo", owner_name="reconciliation")


# ---------------------------------------------------------------------------
# Interaction: both fixes together
# ---------------------------------------------------------------------------


class TestBothFixesInteraction:
    """
    Combined test: reconciliation acquires lock (Fix 1) and refresh correctly
    skips the git repo because of the lock (Fix 2). This is the core race
    condition scenario from Bug #239.
    """

    def test_refresh_skips_while_reconciliation_holds_lock(
        self,
        golden_repos_dir,
        mock_config_source,
        mock_query_tracker,
        mock_cleanup_manager,
        mock_registry,
        alias_manager,
        real_registry,
    ):
        """
        Full race condition scenario:

        1. Reconciliation starts restoring a missing master via reverse CoW (Fix 1 acquires lock)
        2. RefreshScheduler timer fires and calls _execute_refresh() for the same git repo
        3. Fix 2: _execute_refresh() detects the write lock and skips WITHOUT snapshotting
        4. Reconciliation finishes and releases the lock
        5. Next refresh cycle proceeds normally

        This test validates both fixes work together as an integrated system.
        """
        # Create a git repo with alias and registry entry for _execute_refresh.
        # NOTE: Do NOT create remote_repo_dir on disk — the test simulates the
        # startup scenario where the master directory is MISSING and must be
        # restored by reconcile_golden_repos() via reverse CoW clone.
        repo_name = "my-repo"
        alias_name = f"{repo_name}-global"
        remote_repo_dir = golden_repos_dir / repo_name
        alias_manager.create_alias(alias_name, str(remote_repo_dir))
        real_registry.register_global_repo(
            repo_name,
            alias_name,
            "git@github.com:org/my-repo.git",
            str(remote_repo_dir),
        )

        # Create versioned snapshot so reconciliation would try to restore
        (golden_repos_dir / ".versioned" / repo_name / "v_9000000").mkdir(parents=True)

        # Scheduler that has BOTH the mock registry (for reconciliation list) and
        # the real registry (for _execute_refresh lookup). We use the real registry
        # for _execute_refresh since that needs the actual entry.
        reconcile_scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=mock_query_tracker,
            cleanup_manager=mock_cleanup_manager,
            registry=mock_registry,
        )

        refresh_scheduler = RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=mock_config_source,
            query_tracker=mock_query_tracker,
            cleanup_manager=mock_cleanup_manager,
            registry=real_registry,
        )

        # Both schedulers share the same golden_repos_dir, so write locks are shared
        # (they use the same file system for lock files)

        mock_registry.list_global_repos.return_value = [
            {"alias_name": alias_name, "repo_url": "git@github.com:org/my-repo.git"},
        ]

        refresh_was_skipped = []
        git_pull_was_called = []

        # Intercept _restore_master_from_versioned to simulate the race:
        # while restoring, check that the refresh skips
        original_restore = reconcile_scheduler._restore_master_from_versioned

        def race_condition_restore(alias_name_arg, master_path):
            # At this point reconciliation holds the write lock (Fix 1)
            # Simulate the refresh scheduler timer firing concurrently:
            with patch.object(refresh_scheduler, "_detect_existing_indexes", return_value={}), \
                 patch.object(refresh_scheduler, "_reconcile_registry_with_filesystem"), \
                 patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_git:
                mock_git.return_value.has_changes.return_value = False

                refresh_result = refresh_scheduler._execute_refresh(alias_name)

                if "skip" in refresh_result.get("message", "").lower() or \
                   "lock" in refresh_result.get("message", "").lower():
                    refresh_was_skipped.append(True)
                else:
                    refresh_was_skipped.append(False)

                if mock_git.called:
                    git_pull_was_called.append(True)

            # Simulate actual restore
            master_path.mkdir(parents=True, exist_ok=True)
            return True

        with patch.object(reconcile_scheduler, "_restore_master_from_versioned",
                          side_effect=race_condition_restore), \
             patch("subprocess.run", side_effect=_make_subprocess_mock()):
            reconcile_scheduler.reconcile_golden_repos()

        assert len(refresh_was_skipped) >= 1, (
            "race_condition_restore must have been called (master was missing)"
        )
        assert all(refresh_was_skipped), (
            f"Refresh must have been SKIPPED while reconciliation held the write lock. "
            f"Got skip results: {refresh_was_skipped}"
        )
        assert len(git_pull_was_called) == 0, (
            "GitPullUpdater must NOT have been called during reconciliation lock hold"
        )
