"""Bug #1570 Half 2: reclaim already-leaked `.versioned/{alias}/` namespaces.

Bug #1567's orphan sweep fail-closed-skips any namespace whose alias
pointer is missing/unreadable -- correct when the repo still exists (a
transient pointer read failure must never authorize deletion of live
data), but that same skip is what permanently strands a namespace whose
owning golden repo was actually REMOVED (removal deletes the pointer).

A namespace under `.versioned/` whose base clone AND alias pointer are
BOTH absent, AND whose alias is not a `golden_repos` registry row, has no
live target by definition -- every snapshot in it is dead. This module
adds that strictly-conjunctive discriminator so such a namespace is fully
reclaimed instead of skipped forever, while a namespace that is merely
missing its pointer but is STILL REGISTERED continues to be skipped
exactly as before (module docstring's core safety property).

Uses a REAL GoldenRepoManager (real SQLite backend) as the registry
source of truth, a REAL AliasManager, a REAL VersionedSnapshotManager
(local CoW mode), and REAL directories on disk standing in for versioned
snapshots and base clones -- no mocks. Reaching into
`golden_repo_manager._sqlite_backend` and `cleanup_manager
._process_cleanup_queue()` mirrors the established precedent elsewhere in
this suite (e.g. test_golden_repo_manager_global_orphan_1523.py,
test_cleanup_manager_min_retention_age_1457.py) for white-box
verification that a real, synchronous deletion actually happened on disk.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.versioned_snapshot_reconciler import (
    reconcile_versioned_snapshots,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: keep_last=1 -- irrelevant to Half 2 (no live pointer exists to anchor
#: ts_live), kept minimal for parity with the sibling Bug #1567 test files.
KEEP_LAST_MINIMAL = 1

#: Arbitrary, distinct fake creation timestamps for two snapshots in the
#: same namespace -- their absolute values carry no meaning.
SNAPSHOT_TS_OLDER = 1000
SNAPSHOT_TS_NEWER = 2000


@pytest.fixture
def golden_repo_manager():
    with tempfile.TemporaryDirectory() as data_dir:
        mgr = GoldenRepoManager(data_dir=data_dir)
        DatabaseSchema(mgr.db_path).initialize_database()
        yield mgr


def _make_env(golden_repo_manager: GoldenRepoManager):
    golden_repos_dir = golden_repo_manager.golden_repos_dir
    aliases_dir = f"{golden_repos_dir}/aliases"
    alias_manager = AliasManager(aliases_dir)
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(golden_repos_dir))
    cleanup_manager = CleanupManager(
        query_tracker=QueryTracker(), min_retention_age_seconds=0.0
    )
    return golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager


def _make_snapshot_dir(golden_repos_dir, bare_namespace: str, ts: int) -> str:
    path = Path(golden_repos_dir) / ".versioned" / bare_namespace / f"v_{ts}"
    path.mkdir(parents=True)
    return str(path)


def test_genuinely_orphaned_namespace_is_fully_reclaimed(golden_repo_manager):
    """No pointer, no base clone, not registered -- every snapshot in the
    namespace must be scheduled AND actually deleted (via the same
    refcount+age-gated CleanupManager the supersession path already
    uses)."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        golden_repo_manager
    )
    bare_ns = "removed-long-ago"
    snap_a = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS_OLDER)
    snap_b = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS_NEWER)
    # No alias pointer written. No base clone directory created. Not
    # registered in golden_repo_manager (nothing was added_repo'd for it).

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        golden_repo_manager=golden_repo_manager,
    )

    assert bare_ns not in result.skipped_namespaces
    assert snap_a in result.scheduled_paths
    assert snap_b in result.scheduled_paths
    assert snap_a in cleanup_manager.get_pending_cleanups()
    assert snap_b in cleanup_manager.get_pending_cleanups()

    # Prove real deletion, not just scheduling.
    cleanup_manager._process_cleanup_queue()
    assert not Path(snap_a).exists()
    assert not Path(snap_b).exists()


def test_still_registered_repo_with_missing_pointer_is_still_skipped(
    golden_repo_manager,
):
    """The repo is STILL REGISTERED (a golden_repos row exists) even though
    its alias pointer happens to be missing/unreadable right now -- this
    must remain a fail-closed skip with zero deletions, never conflated
    with genuine removal."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        golden_repo_manager
    )
    bare_ns = "still-registered-repo"
    snap_a = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS_OLDER)

    golden_repo_manager._sqlite_backend.add_repo(
        alias=bare_ns,
        repo_url=f"https://github.com/test/{bare_ns}.git",
        default_branch="main",
        clone_path=f"{golden_repos_dir}/{bare_ns}",
        created_at="2026-01-01T00:00:00+00:00",
    )
    # No alias pointer written -- simulates a transient pointer-read gap
    # on a repo that genuinely still exists.

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        golden_repo_manager=golden_repo_manager,
    )

    assert bare_ns in result.skipped_namespaces
    assert result.scheduled_paths == []
    assert cleanup_manager.get_pending_cleanups() == set()
    assert Path(snap_a).exists()


def test_base_clone_still_present_blocks_reclaim(golden_repo_manager):
    """Not registered, no pointer -- but a base clone directory still
    exists on disk. The base-clone-absence conjunct must block reclaim
    even though the other two conjuncts hold."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        golden_repo_manager
    )
    bare_ns = "clone-still-here"
    snap_a = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS_OLDER)

    base_clone = Path(golden_repos_dir) / bare_ns
    base_clone.mkdir(parents=True)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        golden_repo_manager=golden_repo_manager,
    )

    assert bare_ns in result.skipped_namespaces
    assert result.scheduled_paths == []
    assert Path(snap_a).exists()


def test_omitting_golden_repo_manager_preserves_existing_skip_behavior(
    golden_repo_manager,
):
    """Calling reconcile_versioned_snapshots WITHOUT golden_repo_manager
    (every pre-#1570 caller/test) must be byte-identical to before: a
    missing pointer always fail-closed-skips, since there is no registry
    signal available to confirm genuine removal."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        golden_repo_manager
    )
    bare_ns = "no-registry-signal-available"
    snap_a = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS_OLDER)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    assert bare_ns in result.skipped_namespaces
    assert result.scheduled_paths == []
    assert Path(snap_a).exists()
