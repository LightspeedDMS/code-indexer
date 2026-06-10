"""Bug #1084 Phase A5: CleanupManager performs BACKEND-correct deletion behind
its existing QueryTracker refcount gate.

Before this fix CleanupManager only ran ``shutil.rmtree`` — which on cow-daemon
leaves a ghost row in the daemon's SQLite registry and on ONTAP leaks the
FlexClone volume. Now CleanupManager is handed the VersionedSnapshotManager and
its deletion step calls ``snapshot_manager.delete_snapshot()`` for snapshot-shaped
paths (daemon DELETE / FlexClone free / local rmtree-inside-the-manager), while
keeping rmtree for non-snapshot local paths.

CRITICAL invariant preserved: deletion NEVER happens while QueryTracker holds a
non-zero refcount; backoff + circuit breaker stay intact.
"""

from unittest.mock import MagicMock

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker


def _make_snapshot_manager(is_snapshot_for=None):
    """Mock VersionedSnapshotManager. is_versioned_snapshot returns True for paths
    in *is_snapshot_for* (a set), False otherwise."""
    sm = MagicMock()
    snapshot_paths = set(is_snapshot_for or [])
    sm.is_versioned_snapshot.side_effect = lambda p: p in snapshot_paths
    sm.delete_snapshot.return_value = True
    return sm


class TestSnapshotManagerWiring:
    def test_set_snapshot_manager_accepts_manager(self):
        cm = CleanupManager(query_tracker=QueryTracker())
        sm = _make_snapshot_manager()
        cm.set_snapshot_manager(sm)
        assert cm._snapshot_manager is sm


class TestBackendDeletionBehindRefcountGate:
    def test_held_ref_defers_backend_deletion(self):
        """A non-zero QueryTracker refcount must DEFER deletion (no backend call)."""
        qt = QueryTracker()
        cow_path = "/mnt/cow-storage/.versioned/repo/v_1700000000"
        sm = _make_snapshot_manager(is_snapshot_for={cow_path})

        cm = CleanupManager(query_tracker=qt)
        cm.set_snapshot_manager(sm)
        cm.schedule_cleanup(cow_path)

        # Hold a reference — simulates an in-flight NFS query.
        qt.increment_ref(cow_path)

        cm._process_cleanup_queue()

        # Deletion deferred: neither backend delete nor rmtree happened, path stays queued.
        sm.delete_snapshot.assert_not_called()
        assert cow_path in cm.get_pending_cleanups()

    def test_release_triggers_backend_deletion_not_rmtree(self):
        """Releasing the ref triggers snapshot_manager.delete_snapshot (NOT rmtree)
        for a cow-shaped snapshot path."""
        qt = QueryTracker()
        cow_path = "/mnt/cow-storage/.versioned/repo/v_1700000000"
        sm = _make_snapshot_manager(is_snapshot_for={cow_path})

        cm = CleanupManager(query_tracker=qt)
        cm.set_snapshot_manager(sm)
        cm.schedule_cleanup(cow_path)

        # Hold then release.
        qt.increment_ref(cow_path)
        cm._process_cleanup_queue()
        sm.delete_snapshot.assert_not_called()

        qt.decrement_ref(cow_path)

        # _robust_delete must NOT be used for backend-managed snapshots — assert it
        # is never called by patching it to raise if invoked.
        rmtree_called = {"hit": False}
        original = cm._robust_delete

        def _tripwire(path):  # pragma: no cover - asserts non-invocation
            rmtree_called["hit"] = True
            return original(path)

        cm._robust_delete = _tripwire  # type: ignore[method-assign]

        cm._process_cleanup_queue()

        sm.delete_snapshot.assert_called_once()
        # The version_path argument must be the cow path.
        _, kwargs = sm.delete_snapshot.call_args
        args = sm.delete_snapshot.call_args[0]
        passed_path = kwargs.get("version_path", args[-1] if args else None)
        assert passed_path == cow_path
        assert rmtree_called["hit"] is False
        # Path is removed from the queue after successful deletion.
        assert cow_path not in cm.get_pending_cleanups()

    def test_non_snapshot_local_path_uses_rmtree(self, tmp_path):
        """A local directory that is NOT a versioned snapshot still deletes via rmtree
        (no snapshot_manager backend call)."""
        qt = QueryTracker()
        local_dir = tmp_path / "some-old-index"
        local_dir.mkdir()
        (local_dir / "f.txt").write_text("x")
        local_path = str(local_dir)

        sm = _make_snapshot_manager(is_snapshot_for=set())  # nothing is a snapshot

        cm = CleanupManager(query_tracker=qt)
        cm.set_snapshot_manager(sm)
        cm.schedule_cleanup(local_path)

        cm._process_cleanup_queue()

        sm.delete_snapshot.assert_not_called()
        assert not local_dir.exists()
        assert local_path not in cm.get_pending_cleanups()

    def test_no_snapshot_manager_falls_back_to_rmtree(self, tmp_path):
        """When no snapshot_manager is wired, behavior is unchanged (rmtree)."""
        qt = QueryTracker()
        local_dir = tmp_path / "idx"
        local_dir.mkdir()
        local_path = str(local_dir)

        cm = CleanupManager(query_tracker=qt)  # no set_snapshot_manager
        cm.schedule_cleanup(local_path)

        cm._process_cleanup_queue()

        assert not local_dir.exists()


class TestBackendDeletionFailureBackoff:
    def test_backend_delete_failure_records_failure_and_keeps_queued(self):
        """A backend delete that raises must be recorded as a failure (backoff/circuit
        breaker preserved) and the path stays queued for retry."""
        qt = QueryTracker()
        cow_path = "/mnt/cow-storage/.versioned/repo/v_1700000000"
        sm = _make_snapshot_manager(is_snapshot_for={cow_path})
        sm.delete_snapshot.side_effect = RuntimeError("daemon unreachable")

        cm = CleanupManager(query_tracker=qt)
        cm.set_snapshot_manager(sm)
        cm.schedule_cleanup(cow_path)

        cm._process_cleanup_queue()

        # Failure recorded -> backoff scheduled; path still queued (not silently dropped).
        assert cm._get_failure_count(cow_path) == 1
        assert cow_path in cm.get_pending_cleanups()
