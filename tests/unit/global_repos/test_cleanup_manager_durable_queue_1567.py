"""Bug #1567: CleanupManager's pending-deletion queue must be durable.

Prior to this fix, CleanupManager held its pending-deletion queue ONLY in
per-process dictionaries stamped with time.monotonic(). Any process
restart or worker recycle inside the minimum-retention-age window silently
discarded the scheduled deletion, and nothing ever reaped the orphan
afterward -- measured live: 229 leaked snapshots for one repo (~120GB).

These tests use a REAL SQLite-backed GoldenRepoMetadataSqliteBackend (no
mocking of the store) and a real filesystem directory to prove:
  (a) a scheduled deletion SURVIVES a simulated process restart -- a fresh
      CleanupManager constructed against the SAME backend recovers the
      pending entry and still honours its retention floor.
  (b) scheduled_at is a WALL-CLOCK timestamp (time.time()), not
      time.monotonic() -- meaningful across process boundaries.
"""

from __future__ import annotations

import os
import time

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


def _make_backend(tmp_path):
    db_path = str(tmp_path / "golden_repos_metadata.db")
    backend = GoldenRepoMetadataSqliteBackend(db_path)
    backend.ensure_table_exists()
    return backend


def test_scheduled_deletion_survives_a_simulated_process_restart(tmp_path):
    target = tmp_path / "superseded-version"
    target.mkdir()
    backend = _make_backend(tmp_path)

    # "Process 1": schedules a cleanup, then the process dies (its
    # in-process dicts are gone -- we simply drop the reference).
    tracker_1 = QueryTracker()
    manager_1 = CleanupManager(
        query_tracker=tracker_1,
        min_retention_age_seconds=999.0,
        persistence_backend=backend,
    )
    manager_1.schedule_cleanup(str(target))
    assert str(target) in manager_1.get_pending_cleanups()
    del manager_1
    del tracker_1

    # "Process 2": a FRESH CleanupManager against the SAME durable backend
    # (simulating a restart/worker recycle) must recover the pending entry.
    tracker_2 = QueryTracker()
    manager_2 = CleanupManager(
        query_tracker=tracker_2,
        min_retention_age_seconds=999.0,
        persistence_backend=backend,
    )

    assert str(target) in manager_2.get_pending_cleanups(), (
        "a scheduled deletion must survive a process restart when a "
        "durable persistence_backend is wired"
    )

    # The retention floor must still be honoured post-restart: refcount is
    # zero, but the (very long) minimum retention age has not elapsed, so
    # the recovered entry must NOT be deleted yet.
    manager_2._process_cleanup_queue()
    assert target.exists(), (
        "the minimum-retention-age floor must survive the restart -- a "
        "freshly-hydrated entry must not be treated as newly scheduled"
    )
    assert str(target) in manager_2.get_pending_cleanups()


def test_scheduled_at_is_a_wall_clock_timestamp_not_monotonic(tmp_path):
    target = tmp_path / "superseded-version"
    target.mkdir()
    backend = _make_backend(tmp_path)

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker, persistence_backend=backend)

    before = time.time()
    manager.schedule_cleanup(str(target))
    after = time.time()

    rows = backend.list_cleanup_pending_deletions()
    assert len(rows) == 1
    scheduled_at = rows[0]["scheduled_at"]
    # A wall-clock epoch-seconds value must land near time.time(), NOT near
    # time.monotonic() (which starts near 0 or an arbitrary reference point
    # unrelated to the epoch and would fail this bound).
    assert before - 1.0 <= scheduled_at <= after + 1.0


def test_durable_deletion_eventually_removes_the_row_after_successful_delete(
    tmp_path,
):
    target = tmp_path / "superseded-version"
    target.mkdir()
    backend = _make_backend(tmp_path)

    tracker = QueryTracker()
    manager = CleanupManager(
        query_tracker=tracker,
        min_retention_age_seconds=0.0,
        persistence_backend=backend,
    )
    manager.schedule_cleanup(str(target))
    assert backend.list_cleanup_pending_deletions() != []

    manager._process_cleanup_queue()

    assert not target.exists()
    assert backend.list_cleanup_pending_deletions() == [], (
        "the durable row must be removed once the path is actually deleted "
        "-- otherwise a restart would resurrect an already-gone path forever"
    )


def test_set_persistence_backend_hydrates_pending_entries_post_hoc(tmp_path):
    """Mirrors set_snapshot_manager's post-construction wiring pattern:
    the backend may be wired AFTER construction (as lifespan.py does), and
    wiring it must immediately hydrate whatever is already durably
    pending."""
    target = tmp_path / "superseded-version"
    target.mkdir()
    backend = _make_backend(tmp_path)
    backend.schedule_cleanup_deletion(str(target), time.time())

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker)
    assert manager.get_pending_cleanups() == set()

    manager.set_persistence_backend(backend)

    assert str(target) in manager.get_pending_cleanups()


_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="permission checks are bypassed for a root test runner"
)
def test_circuit_breaker_trip_does_not_delete_the_durable_row(tmp_path):
    """Codex-review finding: a durable queue must NOT delete a row after
    MAX_FAILURES. If the circuit breaker both drops the in-process queue
    entry AND removes the durable row, a genuinely-stuck deletion (e.g. a
    permission issue) is abandoned FOREVER -- reintroducing the exact
    leak class Bug #1567 exists to close, just via a different trigger.
    The row must survive so the NEXT process/hydrate gets a fresh
    failure budget to retry (retry-after via the natural restart cycle,
    since failure_count itself is intentionally NOT persisted)."""
    target = tmp_path / "undeletable-version"
    target.mkdir()
    (target / "child.txt").write_text("x")
    os.chmod(str(target), 0o555)  # no write -- rmtree cannot unlink children

    backend = _make_backend(tmp_path)
    tracker = QueryTracker()
    manager = CleanupManager(
        query_tracker=tracker,
        min_retention_age_seconds=0.0,
        persistence_backend=backend,
    )
    manager.schedule_cleanup(str(target))

    try:
        # MAX_FAILURES attempts to actually DRIVE failure_count up to
        # MAX_FAILURES, plus ONE more call so the circuit-breaker check
        # (evaluated at the START of _process_cleanup_queue, against the
        # count left by the PRIOR call) actually fires.
        for _ in range(CleanupManager.MAX_FAILURES + 1):
            # Bypass the exponential backoff between attempts (unrelated
            # to what this test verifies) so each iteration genuinely
            # attempts -- and fails -- the deletion, driving failure_count
            # to MAX_FAILURES within this tight loop.
            with manager._stats_lock:
                manager._next_retry_times[str(target)] = 0.0
            manager._process_cleanup_queue()
    finally:
        os.chmod(str(target), 0o755)

    assert str(target) not in manager.get_pending_cleanups(), (
        "the circuit breaker must still drop the IN-MEMORY queue entry "
        "for this process (stop hot-looping on a known-stuck path)"
    )
    surviving_paths = {
        row["index_path"] for row in backend.list_cleanup_pending_deletions()
    }
    assert str(target) in surviving_paths, (
        "the DURABLE row for this specific target must survive a "
        "circuit-breaker trip -- deleting it would abandon a "
        "genuinely-stuck path forever"
    )
