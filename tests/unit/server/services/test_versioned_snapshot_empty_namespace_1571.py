"""Bug #1571: empty `.versioned/{namespace}/` directories are never removed
after a golden repo is fully deregistered.

Bug #1570's reclaim deletes SNAPSHOTS (`.versioned/{ns}/v_{ts}`) through
`cleanup_manager.schedule_cleanup`, which only ever receives snapshot
paths -- nothing owns the enclosing namespace directory itself. Once a
namespace reaches zero snapshots (a fully-removed repo), the empty
directory survives forever: sweeps, restarts, the cleanup thread.

Fix (sweep-side, per the issue's recommended approach): on a LATER sweep
pass, when a namespace directory is observed to be already-empty on disk
right now, remove it via `os.rmdir` (never `shutil.rmtree` -- its own
"fails if non-empty" semantics are the safety net, not a Python-level
`os.listdir() == []` check performed separately). This is naturally
idempotent: at scheduling time (the SAME pass that calls
`cleanup_manager.schedule_cleanup`) the snapshots are still physically on
disk (both the reconciler's own age floor and CleanupManager's
`min_retention_age_seconds` gate deletion), so a namespace only ever
looks empty to a subsequent sweep after the cleanup thread has actually
removed every snapshot in it.

This new removal step must be attempted unconditionally, per namespace,
BEFORE (and independent of) the pre-existing "no readable alias pointer"
fail-closed skip -- otherwise a namespace with no pointer (exactly the
leaked shape this bug reports) would never reach it. The only gate on
removal itself is (a) `os.rmdir`'s own atomic emptiness check and (b)
whether a governing alias pointer still resolves for this namespace.

Uses REAL directories, a REAL `AliasManager`, a REAL
`VersionedSnapshotManager` (local CoW mode), and a REAL `CleanupManager`
-- no mocks, mirroring the established pattern in
test_versioned_snapshot_reconciler_reclaim_1570.py. Assertions are made
against on-disk filesystem state PLUS the sweep's own result object
(`result.aborted`), every call site capturing and checking it -- never
discarded.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.versioned_snapshot_reconciler import (
    reconcile_versioned_snapshots,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: Arbitrary fake creation timestamp -- absolute value carries no meaning.
SNAPSHOT_TS = 1000

#: Arbitrary fake timestamp for a dangling pointer target that never
#: physically exists on disk -- absolute value carries no meaning.
DANGLING_SNAPSHOT_TS = 999


@pytest.fixture
def env():
    with tempfile.TemporaryDirectory() as golden_repos_dir:
        aliases_dir = f"{golden_repos_dir}/aliases"
        alias_manager = AliasManager(aliases_dir)
        snapshot_manager = VersionedSnapshotManager(versioned_base=golden_repos_dir)
        cleanup_manager = CleanupManager(
            query_tracker=QueryTracker(), min_retention_age_seconds=0.0
        )
        yield golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager


def _make_empty_namespace_dir(golden_repos_dir: str, bare_namespace: str) -> Path:
    path = Path(golden_repos_dir) / ".versioned" / bare_namespace
    path.mkdir(parents=True)
    return path


def _make_snapshot_dir(golden_repos_dir: str, bare_namespace: str, ts: int) -> Path:
    path = Path(golden_repos_dir) / ".versioned" / bare_namespace / f"v_{ts}"
    path.mkdir(parents=True)
    return path


def test_empty_namespace_with_no_pointer_is_removed(env):
    """The exact leak reported live: a namespace whose snapshots have all
    already been deleted (by a fully-deregistered repo's reclaim) has
    nothing left inside it and no alias pointer -- the sweep must remove
    the now-empty directory itself.

    Against CURRENT (pre-fix) code this fails: nothing ever removes the
    namespace directory, only its snapshots -- the directory survives
    the sweep untouched.
    """
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = env
    bare_ns = "fully-removed-repo"
    empty_dir = _make_empty_namespace_dir(golden_repos_dir, bare_ns)
    assert empty_dir.exists()

    result = reconcile_versioned_snapshots(
        golden_repos_dir,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
    )

    assert result.aborted is False
    assert not empty_dir.exists(), (
        "empty namespace directory must be removed by the sweep once it "
        "has zero snapshots and no alias pointer"
    )


def test_namespace_with_a_real_snapshot_is_never_touched(env):
    """A namespace that still holds a real, on-disk snapshot must never
    be removed, regardless of pointer state -- os.rmdir's own
    "fails if non-empty" semantics are the safety net."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = env
    bare_ns = "still-has-a-snapshot"
    snapshot_dir = _make_snapshot_dir(golden_repos_dir, bare_ns, SNAPSHOT_TS)
    namespace_dir = snapshot_dir.parent
    assert namespace_dir.exists()

    result = reconcile_versioned_snapshots(
        golden_repos_dir,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
    )

    assert result.aborted is False
    assert namespace_dir.exists(), "namespace holding real content must survive"
    assert snapshot_dir.exists(), "the snapshot itself must never be touched here"


def test_namespace_with_unrecognized_content_survives_a_failed_rmdir(env):
    """A namespace directory can hold real, on-disk content that the
    reconciler's own snapshot-glob logic does not recognize as a
    `v_<ts>` snapshot (so, from `list_snapshots`'s point of view, the
    namespace looks empty) while still being physically non-empty on
    disk. The new removal step is attempted unconditionally -- including
    for a namespace with no readable pointer, exactly like test 1 above
    -- so `os.rmdir` must be reached and must fail (ENOTEMPTY); that
    failure must be swallowed as non-fatal: no exception escapes, the
    sweep completes normally, and the directory plus its unrecognized
    content survive untouched. This proves the correctness of relying on
    `os.rmdir`'s own atomic emptiness check as the sole safety net,
    rather than a separate Python-level "is this empty" computation that
    could disagree with it."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = env
    bare_ns = "unrecognized-content"
    namespace_dir = _make_empty_namespace_dir(golden_repos_dir, bare_ns)
    unrecognized_entry = namespace_dir / ".in-progress-write"
    unrecognized_entry.mkdir()

    result = reconcile_versioned_snapshots(
        golden_repos_dir,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
    )

    assert result.aborted is False
    assert namespace_dir.exists(), "non-empty directory must survive a failed rmdir"
    assert unrecognized_entry.exists()


def test_empty_namespace_with_a_dangling_pointer_is_never_removed(env):
    """A namespace can be physically empty on disk right now yet still
    have a resolvable (even dangling -- target already deleted) alias
    pointer naming it. That pointer must block removal: the very next
    refresh could legitimately recreate a snapshot under this namespace
    name."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = env
    bare_ns = "dangling-pointer-repo"
    empty_dir = _make_empty_namespace_dir(golden_repos_dir, bare_ns)
    dangling_target = str(
        Path(golden_repos_dir) / ".versioned" / bare_ns / f"v_{DANGLING_SNAPSHOT_TS}"
    )
    alias_manager.create_alias(f"{bare_ns}-global", dangling_target)

    result = reconcile_versioned_snapshots(
        golden_repos_dir,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
    )

    assert result.aborted is False
    assert empty_dir.exists(), (
        "a namespace with a live (even dangling) alias pointer must never "
        "be removed just because it is currently empty on disk"
    )
