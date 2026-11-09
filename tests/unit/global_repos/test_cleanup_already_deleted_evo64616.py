"""issue-64616: index_cleanup is idempotent -- already gone is deleted.

On tools-test two pods ran the same cleanup: one deleted
``.versioned/knowledge-base/v_1792503817``, the other's job then FAILED with
"Failed to delete clone '...': [Errno 2] No such file or directory".

Why both: every node hydrates the same durable pending-deletion row; the
cluster claim serialises the deletes but cannot unqueue a peer's copy, so the
second node deleted again after the first released its claim. Now:

* a node that knew the path had a durable row, and finds the row gone after
  claiming it, drops it as "deleted by a peer" (job completed, not failed);
* every delete treats a tree that vanished under it -- before or mid-walk --
  as deleted: ``LocalCloneBackend`` (also behind ``DelegatedCloneBackend``),
  the snapshot manager's CoW fallback, and workspace-mcp's
  ``/clone-path/delete`` answering 404 for a missing path.

Real SQLite durable queue, real JobTracker, real directories.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.snapshot_deletion_errors import (
    remove_tree_already_gone_ok,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.clone_backend import (
    DelegatedCloneBackend,
    LocalCloneBackend,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)

_REAL_RMTREE = shutil.rmtree


def _tree(root: Path) -> Path:
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f.txt").write_text("x")
    (root / "g.txt").write_text("y")
    return root


def _vanishing_rmtree(times: int) -> Any:
    """rmtree whose first *times* calls lose a race: the tree is removed by
    'another pod' and the call raises ENOENT mid-walk."""
    calls: List[str] = []

    def fake(path: str, *a: Any, **kw: Any) -> None:
        calls.append(path)
        if len(calls) <= times:
            _REAL_RMTREE(path)
            raise FileNotFoundError(2, "No such file or directory", path + "/a/b")
        _REAL_RMTREE(path, *a, **kw)

    return fake


# ── the shared helper ────────────────────────────────────────────────────────


def test_a_tree_removed_mid_walk_by_another_deleter_is_deleted(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "v_1")
    with patch("shutil.rmtree", _vanishing_rmtree(1)):
        remove_tree_already_gone_ok(tree)
    assert not tree.exists()


def test_a_partly_removed_tree_is_finished(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "v_1")
    calls: List[str] = []

    def partial(path: str, *a: Any, **kw: Any) -> None:
        calls.append(path)
        if len(calls) == 1:
            _REAL_RMTREE(Path(path) / "a")  # a peer removed part of it
            raise FileNotFoundError(2, "No such file or directory")
        _REAL_RMTREE(path)

    with patch("shutil.rmtree", partial):
        remove_tree_already_gone_ok(tree)
    assert not tree.exists() and len(calls) == 2


def test_an_enoent_that_persists_while_the_tree_exists_raises(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "v_1")
    with (
        patch("shutil.rmtree", side_effect=FileNotFoundError(2, "gone?")),
        pytest.raises(FileNotFoundError),
    ):
        remove_tree_already_gone_ok(tree)


def test_other_errors_raise_unchanged(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "v_1")
    with (
        patch("shutil.rmtree", side_effect=PermissionError(13, "denied")),
        pytest.raises(PermissionError),
    ):
        remove_tree_already_gone_ok(tree)


# ── the backends ─────────────────────────────────────────────────────────────


def test_local_clone_backend_delete_lost_race_is_success(tmp_path: Path) -> None:
    tree = _tree(tmp_path / ".versioned" / "kb" / "v_1")
    with patch("shutil.rmtree", _vanishing_rmtree(1)):
        assert LocalCloneBackend().delete_clone(str(tree)) is True


def test_cow_snapshot_fallback_delete_lost_race_is_success(tmp_path: Path) -> None:
    tree = _tree(tmp_path / ".versioned" / "kb" / "v_1")
    manager = VersionedSnapshotManager.__new__(VersionedSnapshotManager)
    with patch("shutil.rmtree", _vanishing_rmtree(1)):
        assert manager._delete_cow_snapshot(str(tree)) is True


def _delegated_with_answer(status: int, text: str, body: Any = None) -> Any:
    backend: Any = DelegatedCloneBackend.__new__(DelegatedCloneBackend)
    response = MagicMock(status_code=status, text=text)
    response.json.return_value = body
    setattr(backend, "_timeout", 5)
    setattr(backend, "_post", MagicMock(return_value=response))
    return backend


def test_workspace_mcp_404_for_a_missing_path_is_already_deleted() -> None:
    backend = _delegated_with_answer(
        404, '{"error":"ENOENT: no such file or directory, lstat \'/x\'"}'
    )
    assert backend.delete_working_copy("/data/golden-repos/.versioned/kb/v_1") is False


def test_workspace_mcp_404_for_a_missing_route_still_raises() -> None:
    backend = _delegated_with_answer(
        404, '{"message":"Cannot POST /clone-path/delete","statusCode":404}'
    )
    with pytest.raises(RuntimeError, match="HTTP 404"):
        backend.delete_working_copy("/x")


def test_workspace_mcp_deleted_answer_is_unchanged() -> None:
    backend = _delegated_with_answer(200, "{}", {"deleted": True})
    assert backend.delete_working_copy("/x") is True


# ── two nodes, one durable row ───────────────────────────────────────────────


class _RecordingSnapshots:
    """A real VersionedSnapshotManager over a real LocalCloneBackend that
    records each delete attempt."""

    def __init__(self) -> None:
        self.inner = VersionedSnapshotManager(clone_backend=LocalCloneBackend())
        self.attempts: List[str] = []

    def is_versioned_snapshot(self, path: str) -> bool:
        return "/.versioned/" in path

    def delete_snapshot(self, alias: str, version_path: str) -> bool:
        self.attempts.append(version_path)
        return bool(self.inner.delete_snapshot(alias, version_path))


def _node(db_path: str, base: Path) -> Any:
    from code_indexer.server.services.job_tracker import JobTracker

    tracker = JobTracker(db_path)
    persistence = GoldenRepoMetadataSqliteBackend(db_path)
    persistence.ensure_table_exists()
    manager = CleanupManager(
        query_tracker=QueryTracker(),
        job_tracker=tracker,
        min_retention_age_seconds=0.0,
        persistence_backend=persistence,
    )
    snapshots = _RecordingSnapshots()
    manager.set_snapshot_manager(snapshots)
    lease_root = base / "cidx-meta"
    (base / ".scratch" / "snapshot-reader-leases").mkdir(parents=True, exist_ok=True)
    manager.set_lease_root(lease_root)
    return manager, snapshots, tracker


def test_the_second_node_drops_a_path_its_peer_deleted(tmp_path: Path) -> None:
    db_path = str(tmp_path / "server.db")
    DatabaseSchema(db_path).initialize_database()
    shared = GoldenRepoMetadataSqliteBackend(db_path)
    shared.ensure_table_exists()
    snapshot = _tree(tmp_path / ".versioned" / "knowledge-base" / "v_1792503817")
    shared.schedule_cleanup_deletion(str(snapshot), time.time() - 10_000.0)

    first, first_snapshots, _ = _node(db_path, tmp_path)
    second, second_snapshots, tracker = _node(db_path, tmp_path)
    assert str(snapshot) in second.get_pending_cleanups()

    first._process_cleanup_queue()
    assert not snapshot.exists()
    second._process_cleanup_queue()

    assert first_snapshots.attempts == [str(snapshot)]
    assert second_snapshots.attempts == [], "the peer's deletion was repeated"
    assert second.get_pending_cleanups() == set()
    assert second._get_failure_count(str(snapshot)) == 0
    jobs = tracker.query_jobs(operation_type="index_cleanup", limit=10)
    statuses = sorted(j["status"] for j in jobs)
    assert "failed" not in statuses, jobs


def test_a_path_never_durably_recorded_is_still_deleted(tmp_path: Path) -> None:
    """A schedule whose persist failed is in this node's queue only: the
    missing durable row must never read as "a peer deleted it"."""
    db_path = str(tmp_path / "server.db")
    DatabaseSchema(db_path).initialize_database()
    manager, snapshots, _ = _node(db_path, tmp_path)
    snapshot = _tree(tmp_path / ".versioned" / "kb" / "v_2")
    backend = manager._persistence_backend
    with patch.object(
        backend, "schedule_cleanup_deletion", side_effect=RuntimeError("db down")
    ):
        manager.schedule_cleanup(str(snapshot))
    manager._scheduled_at[str(snapshot)] = time.time() - 10_000.0

    manager._process_cleanup_queue()

    assert snapshots.attempts == [str(snapshot)]
    assert not snapshot.exists()


def test_a_path_this_node_scheduled_and_a_peer_deleted_is_dropped(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "server.db")
    DatabaseSchema(db_path).initialize_database()
    manager, snapshots, _ = _node(db_path, tmp_path)
    snapshot = _tree(tmp_path / ".versioned" / "kb" / "v_3")
    manager.schedule_cleanup(str(snapshot))
    manager._scheduled_at[str(snapshot)] = time.time() - 10_000.0
    # A peer node deletes it and removes the durable row.
    shutil.rmtree(snapshot)
    manager._persistence_backend.remove_cleanup_pending_deletion(str(snapshot))

    manager._process_cleanup_queue()

    assert snapshots.attempts == []
    assert manager.get_pending_cleanups() == set()
