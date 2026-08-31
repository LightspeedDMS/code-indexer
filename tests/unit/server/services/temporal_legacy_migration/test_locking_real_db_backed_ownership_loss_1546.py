"""Issue #1546 AC5, real end-to-end proof: AliasLockOwnershipLostError
from a REAL DB-backed AliasLockStore must reach
temporal_legacy_migration/locking.py's heartbeat thread and mark its
LockLossSignal.

test_locking_1548.py exhaustively covers guarded_by_refresh_lock()'s
behavior against `_FakeRefreshScheduler`/`_FakeWriteLockManager` (a hand
-written fake implementing renew() directly) -- it never exercises the
REAL AliasLockCoordinator + AliasLockStore combination this story
introduces. This test closes that gap with TWO independent, unmodified
real assertions:

1. A direct, un-patched call to `scheduler.write_lock_manager.renew()`
   (the SAME coordinator instance the heartbeat uses) after the tracked
   connection is severed -- proving the EXACT exception type
   (AliasLockOwnershipLostError, not a generic Exception) at the real
   coordinator/store boundary via `pytest.raises`, with no
   instrumentation of any kind.
2. The heartbeat thread inside guarded_by_refresh_lock() independently
   observing the SAME failure and marking the yielded LockLossSignal
   lost -- proving the real exception genuinely propagates through the
   production code path, not just when called directly by the test.

`_HEARTBEAT_INTERVAL_SECONDS` is monkeypatched down from its 15-minute
production value to a bounded test interval -- the SAME precedent
test_locking_1548.py's own `test_guard_renews_the_lock_periodically_
while_body_runs` already establishes for the identical reason. This
patches a plain module-level integer constant, never the code under
test.
"""

from __future__ import annotations

import threading
import time

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.shared_operations import GlobalRepoOperations
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)
from code_indexer.server.services.temporal_legacy_migration import (
    locking as locking_mod,
)
from code_indexer.server.services.temporal_legacy_migration.locking import (
    MIGRATION_OWNER_NAME,
    guarded_by_refresh_lock,
)

_TEST_HEARTBEAT_INTERVAL_SECONDS = 0.02
_WAIT_FOR_LOSS_TIMEOUT_SECONDS = 5.0
_POLL_SLEEP_SECONDS = 0.01


def _make_real_db_backed_scheduler(tmp_path) -> RefreshScheduler:
    golden_repos_dir = tmp_path / "golden-repos"
    store = SqliteAliasLockStore(tmp_path / "alias_locks")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker=query_tracker)
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=GlobalRepoOperations(str(golden_repos_dir)),
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        alias_lock_db_backed_enabled_getter=lambda: True,
        alias_lock_store_resolver=lambda: store,
    )
    # check_refresh_not_in_progress no-ops when _job_tracker is None
    # (already the default here) -- no additional wiring needed.
    return scheduler


def test_real_ownership_loss_marks_lock_loss_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        locking_mod, "_HEARTBEAT_INTERVAL_SECONDS", _TEST_HEARTBEAT_INTERVAL_SECONDS
    )
    scheduler = _make_real_db_backed_scheduler(tmp_path)
    bare_alias = "real-db-alias"

    connection_closed = threading.Event()

    with guarded_by_refresh_lock(scheduler, bare_alias) as lock_loss_signal:
        assert lock_loss_signal.is_lost() is False

        # Simulate a crash: sever the tracked handle's live connection
        # out from under the heartbeat, exactly like
        # AliasLockCoordinator's own crash-recovery tests do.
        handle = scheduler.write_lock_manager._handles[bare_alias]
        handle._connection.close()
        connection_closed.set()

        # Proof 1: a direct, unmodified real call to the SAME coordinator
        # instance -- proves the exact exception type at the real
        # coordinator/store boundary.
        with pytest.raises(AliasLockOwnershipLostError):
            scheduler.write_lock_manager.renew(
                bare_alias, owner_name=MIGRATION_OWNER_NAME
            )

        # Proof 2: the background heartbeat thread independently observes
        # the SAME failure through the production code path and marks
        # the signal lost.
        deadline = time.monotonic() + _WAIT_FOR_LOSS_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not lock_loss_signal.is_lost():
            time.sleep(_POLL_SLEEP_SECONDS)

        assert lock_loss_signal.is_lost() is True, (
            "the real AliasLockOwnershipLostError from the severed "
            "connection must reach the heartbeat and mark the "
            "LockLossSignal lost within the timeout"
        )

    assert connection_closed.is_set()
