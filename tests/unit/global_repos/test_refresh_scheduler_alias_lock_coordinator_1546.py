"""RefreshScheduler wires AliasLockCoordinator as write_lock_manager
(Issue #1546 Phase 2).

`self.write_lock_manager` used to be a bare `WriteLockManager` instance.
It is now an `AliasLockCoordinator` -- every one of the ~8 real call
sites in this codebase already reaches the lock exclusively through
`scheduler.acquire_write_lock()`/`release_write_lock()`/
`is_write_locked()` or `scheduler.write_lock_manager.acquire/release/
renew` directly, so this ONE wiring point rewires all of them onto the
DB-backed mechanism (behind the AliasLockConfig.db_backed_enabled
rollout flag) without any call-site changes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.global_repos.alias_lock_coordinator import AliasLockCoordinator
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.write_lock_manager import WriteLockManager


def _make_scheduler(golden_repos_dir: Path, **kwargs) -> RefreshScheduler:
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker=query_tracker)
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=MagicMock(),
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        **kwargs,
    )


class TestWriteLockManagerIsCoordinator:
    def test_write_lock_manager_is_an_alias_lock_coordinator(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        assert isinstance(scheduler.write_lock_manager, AliasLockCoordinator)

    def test_coordinator_wraps_a_real_write_lock_manager_at_the_same_dir(
        self, tmp_path
    ):
        scheduler = _make_scheduler(tmp_path)
        file_manager = scheduler.write_lock_manager._file_manager
        assert isinstance(file_manager, WriteLockManager)
        assert file_manager._golden_repos_dir == tmp_path

    def test_default_construction_is_byte_identical_file_based_behavior(self, tmp_path):
        """No new params passed -- must behave exactly like the old bare
        WriteLockManager did: acquire creates a real lock file on disk."""
        scheduler = _make_scheduler(tmp_path)
        acquired = scheduler.acquire_write_lock("myalias")
        try:
            assert acquired is True
            assert (tmp_path / ".locks" / "myalias.lock").exists()
        finally:
            scheduler.release_write_lock("myalias")
        assert not (tmp_path / ".locks" / "myalias.lock").exists()


class TestAliasLockRolloutWiring:
    def test_db_backed_getter_and_store_resolver_are_forwarded(self, tmp_path):
        """When the caller supplies a getter/resolver, acquire_write_lock()
        must dispatch through them -- proving the constructor actually
        threads these params into the coordinator rather than dropping
        them."""
        fake_store = MagicMock()
        fake_handle = MagicMock()
        fake_store.try_acquire.return_value = fake_handle

        scheduler = _make_scheduler(
            tmp_path,
            alias_lock_db_backed_enabled_getter=lambda: True,
            alias_lock_store_resolver=lambda: fake_store,
        )

        acquired = scheduler.acquire_write_lock("myalias")
        try:
            assert acquired is True
            fake_store.try_acquire.assert_called_once()
            # Never touched the file mechanism.
            assert not (tmp_path / ".locks" / "myalias.lock").exists()
        finally:
            fake_store.release.return_value = None
            scheduler.release_write_lock("myalias")

    def test_getter_absent_defaults_to_file_based_even_with_resolver_present(
        self, tmp_path
    ):
        """Omitting the getter (e.g. CLI/solo construction paths that
        never pass either kwarg) must stay pure file-based, matching
        AliasLockCoordinator's own `None`-getter default -- never
        acquiring through the DB store (try_acquire). Issue #1546 Fix 2:
        the flag-OFF path now DOES consult the store's authoritative
        is_held() as a cross-mechanism conflict check before granting
        the file lock (a store_resolver is configured here, mirroring
        production once wired) -- configured to report "not held" so
        this test's original file-based-acquisition intent still holds."""
        fake_store = MagicMock()
        fake_store.is_held.return_value = False
        scheduler = _make_scheduler(
            tmp_path, alias_lock_store_resolver=lambda: fake_store
        )

        acquired = scheduler.acquire_write_lock("myalias")
        try:
            assert acquired is True
            assert (tmp_path / ".locks" / "myalias.lock").exists()
            fake_store.try_acquire.assert_not_called()
        finally:
            scheduler.release_write_lock("myalias")
