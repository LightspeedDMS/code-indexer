"""RefreshScheduler.raise_if_write_lock_ownership_lost() -- Issue #1546
AC5's uniform, backend-agnostic ownership-loss checkpoint primitive.

Real lease-holding call sites (activation clone, branch-change's swap,
add-indexes' post-loop snapshot, fleet-migration orchestrator) call this
at each critical phase of a long-running operation that holds the write
lock. It must raise the SAME exception type regardless of which backend
is active:

- DB-backed mode: the tracked handle's store.renew() already raises
  AliasLockOwnershipLostError on loss (zero-rows-affected exact-token
  UPDATE) -- this propagates unwrapped.
- File mode: WriteLockManager.renew() returns False (no lock file, or
  owner/owner_token mismatch) rather than raising -- translated here
  into the SAME AliasLockOwnershipLostError so callers get ONE uniform
  contract, never needing to branch on which backend is active.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)


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


class TestFileModeCheckpoint:
    def test_checkpoint_succeeds_silently_while_lock_is_held(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        assert scheduler.acquire_write_lock("myalias", owner_name="tester") is True
        try:
            scheduler.raise_if_write_lock_ownership_lost("myalias", owner_name="tester")
        finally:
            # release_write_lock() returns None (any owner-mismatch
            # failure is logged internally by the method itself) -- no
            # return value to check here.
            scheduler.release_write_lock("myalias", owner_name="tester")

    def test_checkpoint_raises_when_lock_was_never_held(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        with pytest.raises(AliasLockOwnershipLostError):
            scheduler.raise_if_write_lock_ownership_lost("myalias", owner_name="tester")

    def test_checkpoint_raises_after_lock_released_out_from_under_holder(
        self, tmp_path
    ):
        """Simulates a stale-eviction/owner-mismatch loss: the lock file
        is deleted externally while the holder still thinks it owns it."""
        scheduler = _make_scheduler(tmp_path)
        assert scheduler.acquire_write_lock("myalias", owner_name="tester") is True
        (tmp_path / ".locks" / "myalias.lock").unlink()

        try:
            with pytest.raises(AliasLockOwnershipLostError):
                scheduler.raise_if_write_lock_ownership_lost(
                    "myalias", owner_name="tester"
                )
        finally:
            # release_write_lock() returns None; the lock file is
            # already gone, so WriteLockManager.release() treats this as
            # an idempotent no-op internally -- no return value to check.
            scheduler.release_write_lock("myalias", owner_name="tester")


class TestDbBackedModeCheckpoint:
    def test_checkpoint_succeeds_silently_while_lock_is_held(self, tmp_path):
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        scheduler = _make_scheduler(
            tmp_path,
            alias_lock_db_backed_enabled_getter=lambda: True,
            alias_lock_store_resolver=lambda: store,
        )
        acquired = scheduler.write_lock_manager.acquire(
            "myalias", owner_name="tester", owner_token="tok-1"
        )
        assert acquired is True
        try:
            scheduler.raise_if_write_lock_ownership_lost(
                "myalias", owner_name="tester", owner_token="tok-1"
            )
        finally:
            # release_write_lock() returns None -- no return value to
            # check here.
            scheduler.release_write_lock(
                "myalias", owner_name="tester", owner_token="tok-1"
            )

    def test_checkpoint_raises_on_severed_connection(self, tmp_path):
        """Simulates a crash mid-hold: the connection dies but the
        in-process handle is still tracked."""
        store = SqliteAliasLockStore(tmp_path / "alias_locks")
        scheduler = _make_scheduler(
            tmp_path,
            alias_lock_db_backed_enabled_getter=lambda: True,
            alias_lock_store_resolver=lambda: store,
        )
        acquired = scheduler.write_lock_manager.acquire(
            "myalias", owner_name="tester", owner_token="tok-1"
        )
        assert acquired is True
        handle = scheduler.write_lock_manager._handles["myalias"]
        handle._connection.close()

        try:
            with pytest.raises(AliasLockOwnershipLostError):
                scheduler.raise_if_write_lock_ownership_lost(
                    "myalias", owner_name="tester", owner_token="tok-1"
                )
        finally:
            # release_write_lock() returns None. Internally,
            # AliasLockCoordinator.release() catches
            # AliasLockOwnershipLostError from the dead connection and
            # reports False to release_write_lock() (logged there as a
            # warning) -- it never raises, per its documented contract.
            scheduler.release_write_lock(
                "myalias", owner_name="tester", owner_token="tok-1"
            )
