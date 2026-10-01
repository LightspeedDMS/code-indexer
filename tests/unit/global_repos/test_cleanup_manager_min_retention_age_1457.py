"""CleanupManager minimum-retention-age floor (Story #1457 AC13).

CleanupManager's refcount-zero deletion gate is PROCESS-LOCAL -- it cannot
see another process/node's in-flight refcount. This story's temporal read
pattern (long multi-shard date-range fan-out queries, tightly coupled to the
same refresh cycle that republishes the shards being read) makes this
pre-existing gap materially worse. Fix: a NEW, purely time-based deletion
floor, added IN ADDITION to the existing position-based retention -- a
version cannot be deleted until BOTH (a) its refcount is zero AND (b) a
minimum retention age has elapsed since it was scheduled for cleanup.

These tests use a real filesystem directory and a real QueryTracker (no
mocking of CleanupManager's own deletion logic) to prove the time-based gate
actually withholds and later permits deletion.
"""

from __future__ import annotations

import time

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker


def test_path_not_deleted_before_min_retention_age_even_with_zero_refcount(tmp_path):
    target = tmp_path / "superseded-version"
    target.mkdir()

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker, min_retention_age_seconds=5.0)

    manager.schedule_cleanup(str(target))
    manager._process_cleanup_queue()  # refcount is zero, but age floor not met

    assert target.exists(), (
        "CleanupManager must not delete a scheduled path before the "
        "minimum retention age has elapsed, even when its refcount is zero"
    )
    assert str(target) in manager.get_pending_cleanups()


def test_path_deleted_once_min_retention_age_elapses(tmp_path):
    target = tmp_path / "superseded-version"
    target.mkdir()

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker, min_retention_age_seconds=0.2)

    manager.schedule_cleanup(str(target))
    manager._process_cleanup_queue()  # too soon -- not deleted yet
    assert target.exists()

    time.sleep(0.25)
    manager._process_cleanup_queue()  # both refcount-zero AND age floor now satisfied

    assert not target.exists(), (
        "CleanupManager must delete a scheduled path once BOTH the "
        "refcount-zero gate AND the minimum retention age are satisfied"
    )
    assert str(target) not in manager.get_pending_cleanups()
