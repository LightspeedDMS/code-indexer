"""
Regression tests for Bug #1523 -- global-registry orphans left behind by
remove_golden_repo() when filesystem cleanup fails.

Bug #1317 established the ordering discipline for the LOCAL half of golden
repo removal: the `golden_repos` row is removed BEFORE any on-disk deletion,
so a later filesystem-cleanup failure "can only ever leave a harmless orphan
CLONE (files, no row), never a registry-orphan."

The GLOBAL half of cleanup did NOT follow that discipline.
`GlobalActivator.deactivate_golden_repo()` (global registry entry +
`-global` alias pointer) sat inside an `if cleanup_successful:` branch, so a
`_cleanup_repository_files()` failure skipped it entirely -- even though the
`golden_repos` row had ALREADY been deleted earlier in the same worker. That
produced a permanently wedged state (confirmed on clustered staging):

  * `golden_repos` row: gone (retrying removal reports "not found")
  * global registry entry + `-global` alias: STILL present, still queryable,
    resolving to a clone that is gone or partially deleted
  * clone directory: still present, blocking re-registration of the alias

These tests use the REAL GoldenRepoManager, REAL SQLite backend, REAL
GlobalActivator/AliasManager and the REAL cleanup code path. The primary test
injects a GENUINE filesystem-cleanup failure (a permission-denied
`shutil.rmtree`, achieved by making the clone directory non-writable while it
still holds a file). The ordering test wraps only the external stdlib
dependency `shutil.rmtree` -- delegating to the real implementation -- to
observe global-alias state at the exact moment the unmodified cleanup code
deletes files. Only the BackgroundJobManager (a pure threading/dispatch
concern) is mocked, so the worker closure runs synchronously in the test
thread.
"""

import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.global_activation import GlobalActivator
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GitOperationError,
)


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def manager(temp_data_dir):
    """Real GoldenRepoManager with a full DB schema (incl. global_repos) so
    the REAL GlobalActivator's registry works end-to-end. Only the
    BackgroundJobManager dispatch boundary is mocked."""
    mgr = GoldenRepoManager(data_dir=temp_data_dir)

    mock_bjm = MagicMock(spec=BackgroundJobManager)
    mock_bjm.submit_job.return_value = "test-job-id-1523"
    mgr.background_job_manager = mock_bjm

    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(mgr.db_path).initialize_database()
    return mgr


def captured_worker(manager: GoldenRepoManager):
    """Return the func passed to the most recent submit_job() call."""
    return manager.background_job_manager.submit_job.call_args[1]["func"]


def alias_manager_for(manager: GoldenRepoManager) -> AliasManager:
    return AliasManager(os.path.join(manager.golden_repos_dir, "aliases"))


def register_globally_active_repo(manager: GoldenRepoManager, alias: str) -> str:
    """Register `alias` in the shared backend + in-memory cache with a real,
    non-empty on-disk clone AND a real global activation (registry entry +
    alias pointer file) -- exactly the state a completed add_golden_repo()
    leaves behind for a globally-active repo.
    """
    clone_path = os.path.join(manager.golden_repos_dir, alias)
    os.makedirs(clone_path, exist_ok=True)
    # A real file inside the clone: rmtree must actually unlink something,
    # and that unlink is what the permission failure denies.
    with open(os.path.join(clone_path, "README.md"), "w") as handle:
        handle.write("payload\n")

    golden_repo = GoldenRepo(
        alias=alias,
        repo_url=f"https://github.com/test/{alias}.git",
        default_branch="main",
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
        enable_temporal=False,
        temporal_options=None,
    )
    manager.golden_repos[alias] = golden_repo
    manager._sqlite_backend.add_repo(
        alias=golden_repo.alias,
        repo_url=golden_repo.repo_url,
        default_branch=golden_repo.default_branch,
        clone_path=golden_repo.clone_path,
        created_at=golden_repo.created_at,
        enable_temporal=golden_repo.enable_temporal,
        temporal_options=golden_repo.temporal_options,
    )

    GlobalActivator(manager.golden_repos_dir).activate_golden_repo(
        repo_name=alias,
        repo_url=golden_repo.repo_url,
        clone_path=clone_path,
    )

    # Preconditions: the global half really is present before removal.
    assert manager.is_globally_active(alias)
    assert alias_manager_for(manager).alias_exists(f"{alias}-global")
    return clone_path


class TestGlobalDeactivationNotGatedOnCleanupBug1523:
    """Global deactivation must never be gated on filesystem-cleanup success."""

    def test_global_alias_removed_even_when_filesystem_cleanup_fails(self, manager):
        """Bug #1523: a REAL filesystem-cleanup failure must not leave the
        global registry entry / `-global` alias pointer behind after the
        `golden_repos` row has already been deleted.
        """
        alias = "wedged-repo"
        clone_path = register_globally_active_repo(manager, alias)

        manager.remove_golden_repo(alias)
        background_worker = captured_worker(manager)

        # GENUINE cleanup failure: strip write permission from the clone
        # directory so shutil.rmtree's unlink of README.md raises
        # PermissionError -> _cleanup_filesystem() returns False ->
        # _cleanup_repository_files() returns False. No mocks involved.
        os.chmod(clone_path, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with pytest.raises(GitOperationError) as exc_info:
                background_worker()
        finally:
            os.chmod(clone_path, stat.S_IRWXU)

        # The resource-leak signal is PRESERVED -- this fix changes ordering,
        # not the failure reporting.
        assert "Resource leak detected" in str(exc_info.value)

        # The leak is real: the clone directory genuinely survived.
        assert os.path.exists(clone_path)

        # Local half: row gone from both the shared backend and the cache.
        assert manager._sqlite_backend.get_repo(alias) is None
        assert alias not in manager.golden_repos

        # Global half (the actual bug): registry entry AND alias pointer must
        # be gone too -- otherwise the repo stays advertised/queryable with
        # broken content and can never be removed or re-added.
        assert not manager.is_globally_active(alias)
        assert not alias_manager_for(manager).alias_exists(f"{alias}-global")

    def test_global_deactivation_happens_before_filesystem_cleanup(self, manager):
        """Bug #1523 ordering: global deactivation must already be complete by
        the time the real cleanup code deletes files, so a repo is never
        advertised via its `-global` alias while its clone is disappearing.

        Observes through the external stdlib `shutil.rmtree` dependency (the
        real implementation still performs the deletion); the manager's own
        cleanup methods run entirely unmodified.
        """
        alias = "ordering-repo"
        register_globally_active_repo(manager, alias)
        aliases = alias_manager_for(manager)

        observed = {}
        real_rmtree = shutil.rmtree

        def recording_rmtree(path, *args, **kwargs):
            observed["globally_active"] = manager.is_globally_active(alias)
            observed["alias_exists"] = aliases.alias_exists(f"{alias}-global")
            return real_rmtree(path, *args, **kwargs)

        manager.remove_golden_repo(alias)
        background_worker = captured_worker(manager)

        with patch.object(shutil, "rmtree", side_effect=recording_rmtree):
            result = background_worker()

        assert result["success"] is True
        assert observed, "the real cleanup path never invoked shutil.rmtree"
        assert observed["globally_active"] is False
        assert observed["alias_exists"] is False


class TestSuccessfulRemovalTeardownBug1523:
    """The reordering must not weaken the normal successful-removal teardown."""

    def test_successful_removal_also_removes_global_alias_and_registry_entry(
        self, manager
    ):
        alias = "clean-repo"
        clone_path = register_globally_active_repo(manager, alias)

        manager.remove_golden_repo(alias)
        result = captured_worker(manager)()

        assert result["success"] is True
        assert result["cascade_results"]["global_alias_deleted"] is True
        assert result["cascade_results"]["golden_repo_deleted"] is True
        assert manager._sqlite_backend.get_repo(alias) is None
        assert not os.path.exists(clone_path)
        assert not manager.is_globally_active(alias)
        assert not alias_manager_for(manager).alias_exists(f"{alias}-global")
