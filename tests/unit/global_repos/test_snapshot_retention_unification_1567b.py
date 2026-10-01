"""Bug #1567 Gap 1: unify the LIVE retention path with the reconciler's
ts_live-anchored supersession predicate.

`enforce_snapshot_retention` (this module) runs on EVERY refresh in
production and, before this fix, carried three defects an adversarial
review found in `versioned_snapshot_reconciler.py`'s module docstring:

  (1) STRADDLED READ: `current_target` (caller-supplied) and
      `previous_path` (via `AliasManager.get_previous_path`) came from
      TWO SEPARATE opens of the alias pointer file, which can straddle a
      concurrent `swap_alias`.
  (2) WRITE-MODE REDIRECTION: `AliasManager.read_alias` silently
      redirects a `-global` alias with an active write session to its
      write-mode SOURCE path, so the real live snapshot can be excluded
      from the keep set.
  (3) NO ts_live ANCHOR: a keep-set of {target, previous, N-newest}
      protects the live/in-flight snapshot only by coincidence -- enough
      crash-orphans (each newer than target) and the true live target
      (or an in-flight build not yet swapped in) can fall outside the
      "N newest on disk" window and be scheduled for deletion.

These tests prove the unified implementation (reusing the SAME
ts_live-anchored predicate `versioned_snapshot_reconciler.py` uses,
extracted into this module as the single shared source of truth) closes
all three, while preserving every existing `enforce_snapshot_retention`/
`discover_and_enforce_temporal_retention` test in
`test_snapshot_retention_1457.py`.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.snapshot_retention import enforce_snapshot_retention
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: Zero protection from "newest N of history" -- isolates the ts_live
#: anchor / referenced-pointer protections from incidental N-newest luck.
KEEP_LAST_MINIMAL = 1


def _make_env(tmp_path):
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir()
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(golden_repos_dir))
    cleanup_manager = CleanupManager(query_tracker=QueryTracker())
    return golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager


def _make_snapshot_dir(golden_repos_dir, bare_namespace: str, ts: int) -> str:
    path = golden_repos_dir / ".versioned" / bare_namespace / f"v_{ts}"
    path.mkdir(parents=True)
    return str(path)


# ---------------------------------------------------------------------------
# (a) Straddled read: a swap_alias is genuinely interleaved MID-EXECUTION of
#     enforce_snapshot_retention (deterministically, via a side-effect hook
#     on the exact call the old algorithm used for its SEPARATE second
#     read), combined with an in-flight orphan so the "protect N newest on
#     disk" heuristic cannot accidentally mask the danger. Must protect the
#     live target, the previous target, AND the in-flight orphan.
# ---------------------------------------------------------------------------


def test_straddled_read_concurrent_swap_mid_execution_never_loses_live_target(
    tmp_path,
):
    """Deterministically interleaves a concurrent `swap_alias` into the
    EXACT window the old algorithm's straddled read occupied: the caller's
    `current_target` param is captured BEFORE the swap; the swap itself is
    triggered as a side effect of `AliasManager.get_previous_path` (the
    call the OLD algorithm used for its second, separate read) so it lands
    squarely between the caller's read and whatever the function does with
    it. An in_flight_orphan newer than everything is materialized up
    front so the old algorithm's "protect newest-1-on-disk" heuristic
    consumes that slot instead of the true live target -- otherwise the
    heuristic accidentally masks the bug (the live target, being the most
    recently created snapshot, would otherwise always win the "newest on
    disk" slot regardless of pointer staleness)."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, 100)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, 200)
    previous_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 300)
    alias_manager.create_alias(alias_name, previous_target, repo_name=bare_ns)

    live_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 400)
    in_flight_orphan = _make_snapshot_dir(golden_repos_dir, bare_ns, 500)

    swap_triggered = {"done": False}
    original_get_previous_path = alias_manager.get_previous_path

    def _swap_then_read(name):
        if not swap_triggered["done"]:
            swap_triggered["done"] = True
            alias_manager.swap_alias(
                alias_name, new_target=live_target, old_target=previous_target
            )
        return original_get_previous_path(name)

    with patch.object(alias_manager, "get_previous_path", side_effect=_swap_then_read):
        enforce_snapshot_retention(
            alias_name,
            previous_target,  # caller's current_target, captured pre-swap
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
            retention_keep_last=KEEP_LAST_MINIMAL,
        )

    pending = cleanup_manager.get_pending_cleanups()
    assert live_target not in pending, "the TRUE live target must survive"
    assert previous_target not in pending, "the rollback previous must survive"
    assert in_flight_orphan not in pending, (
        "a snapshot newer than the live target must never be scheduled"
    )
    assert old1 in pending
    assert old2 in pending


def test_straddled_read_no_interleaving_ordering_matches_interleaved_ordering(
    tmp_path,
):
    """The second required ordering: no interleaving at all (swap fully
    completes before enforce_snapshot_retention is ever called, so
    current_target is already fresh). Must produce the IDENTICAL protected
    set as the interleaved ordering above -- the outcome must not depend on
    timing."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, 100)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, 200)
    previous_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 300)
    alias_manager.create_alias(alias_name, previous_target, repo_name=bare_ns)
    live_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 400)
    in_flight_orphan = _make_snapshot_dir(golden_repos_dir, bare_ns, 500)

    # Swap completes BEFORE the call -- current_target is already fresh.
    alias_manager.swap_alias(
        alias_name, new_target=live_target, old_target=previous_target
    )

    enforce_snapshot_retention(
        alias_name,
        live_target,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert live_target not in pending
    assert previous_target not in pending
    assert in_flight_orphan not in pending
    assert old1 in pending
    assert old2 in pending


# ---------------------------------------------------------------------------
# (b) In-flight: a snapshot newer than ts_live (create-before-swap) is
#     NEVER scheduled by the live retention path.
# ---------------------------------------------------------------------------


def test_in_flight_snapshot_newer_than_live_target_never_scheduled(tmp_path):
    """Isolates hole (3) alone: current_target is FRESH (no staleness), but
    an in-flight snapshot newer than the live target exists on disk
    (materialized by `_create_snapshot` before `swap_alias` runs). Must
    never be scheduled regardless of keep_last."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    live_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 1000)
    alias_manager.create_alias(alias_name, live_target, repo_name=bare_ns)

    orphan1 = _make_snapshot_dir(golden_repos_dir, bare_ns, 1100)
    orphan2 = _make_snapshot_dir(golden_repos_dir, bare_ns, 1200)
    orphan3 = _make_snapshot_dir(golden_repos_dir, bare_ns, 1300)

    enforce_snapshot_retention(
        alias_name,
        live_target,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    for orphan in (orphan1, orphan2, orphan3, live_target):
        assert orphan not in pending


# ---------------------------------------------------------------------------
# (c) Write-mode: a -global alias with an active write session must not
#     omit the live snapshot from the keep set.
# ---------------------------------------------------------------------------


def test_write_mode_active_session_does_not_exclude_live_snapshot(tmp_path):
    """AliasManager.read_alias() redirects reads for a `-global` alias with
    an active write-mode session to the write-mode SOURCE path (a live
    working directory, not a versioned snapshot at all). If
    enforce_snapshot_retention used read_alias() anywhere to determine the
    live snapshot, the real target_path/previous_path would never be
    consulted for protection while write-mode is active. The unified
    implementation must read the RAW pointer file directly (never
    read_alias/get_previous_path), so write-mode activity must have ZERO
    effect on which snapshots are protected."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, 100)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, 200)
    previous_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 300)
    alias_manager.create_alias(alias_name, previous_target, repo_name=bare_ns)
    live_target = _make_snapshot_dir(golden_repos_dir, bare_ns, 400)
    alias_manager.swap_alias(
        alias_name, new_target=live_target, old_target=previous_target
    )

    # Activate a write-mode session redirecting reads to an unrelated live
    # working directory -- NOT a versioned snapshot at all.
    write_mode_dir = golden_repos_dir / ".write_mode"
    write_mode_dir.mkdir(parents=True, exist_ok=True)
    live_working_dir = tmp_path / "live-working-copy"
    live_working_dir.mkdir()
    (write_mode_dir / f"{bare_ns}.json").write_text(
        json.dumps({"source_path": str(live_working_dir)})
    )

    # Sanity: confirm write-mode redirection really is active for this
    # alias via AliasManager's own public API (proves the marker is real).
    assert alias_manager.read_alias(alias_name) == str(live_working_dir)

    enforce_snapshot_retention(
        alias_name,
        live_target,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert live_target not in pending
    assert previous_target not in pending
    assert old1 in pending
    assert old2 in pending


# ---------------------------------------------------------------------------
# (d) Path-rooting: a namespace whose current_target is not a genuine
#     versioned snapshot must schedule ZERO -- fail closed, never guess.
#     A namespace correctly SHAPED under a DIFFERENT root (cow-daemon
#     mount point, exercised through the REAL VersionedSnapshotManager /
#     class-name-dispatched backend) must still be correctly unified.
# ---------------------------------------------------------------------------


def test_non_canonical_target_path_schedules_zero(tmp_path):
    """current_target is the master clone path (golden_repos_dir/{repo}) --
    not a v_<ts> snapshot at all. Must fail closed: zero scheduled,
    regardless of what other real snapshots exist for this namespace."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, 100)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, 200)
    master_clone_path = str(golden_repos_dir / bare_ns)
    alias_manager.create_alias(alias_name, master_clone_path, repo_name=bare_ns)

    enforce_snapshot_retention(
        alias_name,
        master_clone_path,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert old1 not in pending
    assert old2 not in pending
    assert pending == set()


class CowDaemonBackend:
    """Duck-typed fake matching the REAL `CowDaemonBackend`'s class NAME
    exactly (`server/storage/shared/clone_backend.py`). This is required,
    not incidental: `VersionedSnapshotManager.list_snapshots`/
    `_backend_mount_point` dispatch by `type(backend).__name__ ==
    "CowDaemonBackend"` (documented in that module as intentional --
    "gated by class name, mirroring the existing backend-name-based
    dispatch"). Naming this fake identically routes the test through the
    REAL production `_list_cow_daemon_snapshots` / `is_versioned_snapshot`
    / `_backend_mount_point` code paths -- only the network call
    (`list_clones`) is faked, not the path-recognition logic itself.
    """

    def __init__(self, mount_point: str, clones_by_namespace):
        self._mount_point = mount_point
        self._clones_by_namespace = clones_by_namespace

    @staticmethod
    def _sanitize_identifier(alias: str) -> str:
        return alias

    def list_clones(self, namespace: str):
        return self._clones_by_namespace.get(namespace, [])


def test_differently_rooted_but_canonically_shaped_target_is_correctly_unified(
    tmp_path,
):
    """Cow-daemon investigation finding: the versioned root for a given
    namespace is NOT always golden_repos_dir -- `VersionedSnapshotManager`
    with a `CowDaemonBackend` roots snapshots at the backend's OWN mount
    point instead (`snapshot_paths.py`'s own documented convention:
    "snapshot_root is golden_repos_dir (local) or mount_point (cow-daemon/
    ONTAP)"). Exercised through the REAL `VersionedSnapshotManager`
    (`clone_backend=` a class-name-matched fake) so `list_snapshots`/
    `is_versioned_snapshot` run genuine production code, not a test
    double's own reimplementation. The unified predicate must not
    hardcode golden_repos_dir as THE root -- it must recognize the
    canonical .versioned/{ns}/v_<ts> SHAPE under whatever root the real
    pointer/snapshot data actually lives. This is the "normalise/resolve
    both sides before comparison" fix, not a weakening: a non-canonical
    shape (previous test) still fails closed."""
    golden_repos_dir, alias_manager, _local_snapshot_manager, cleanup_manager = (
        _make_env(tmp_path)
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    # A root that is DELIBERATELY NOT golden_repos_dir -- simulates a
    # cow-daemon mount point living elsewhere on disk.
    mount_point = str(tmp_path / "mnt-cow-storage")
    ns_dir = tmp_path / "mnt-cow-storage" / ".versioned" / bare_ns
    ns_dir.mkdir(parents=True)

    def _foreign_snapshot(ts: int) -> str:
        (ns_dir / f"v_{ts}").mkdir()
        return str(ns_dir / f"v_{ts}")

    old1 = _foreign_snapshot(100)
    old2 = _foreign_snapshot(200)
    live_target = _foreign_snapshot(300)
    alias_manager.create_alias(alias_name, live_target, repo_name=bare_ns)

    fake_backend = CowDaemonBackend(
        mount_point=mount_point,
        clones_by_namespace={
            bare_ns: [
                {"name": "v_100", "namespace": bare_ns},
                {"name": "v_200", "namespace": bare_ns},
                {"name": "v_300", "namespace": bare_ns},
            ]
        },
    )
    cow_snapshot_manager = VersionedSnapshotManager(
        clone_backend=fake_backend, versioned_base=str(golden_repos_dir)
    )

    enforce_snapshot_retention(
        alias_name,
        live_target,
        snapshot_manager=cow_snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert live_target not in pending
    assert old1 in pending
    assert old2 in pending


def test_min_absolute_age_floor_still_protects_freshly_scheduled_snapshot(tmp_path):
    """The unification must not silently drop the reconciler's independent
    min-absolute-age safety margin: a genuinely superseded snapshot younger
    than the floor must not be scheduled yet."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"

    now = int(time.time())
    just_superseded = _make_snapshot_dir(golden_repos_dir, bare_ns, now)
    live_target = _make_snapshot_dir(golden_repos_dir, bare_ns, now + 10)
    alias_manager.create_alias(alias_name, live_target, repo_name=bare_ns)

    enforce_snapshot_retention(
        alias_name,
        live_target,
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert just_superseded not in pending, (
        "a snapshot younger than the minimum retention-age floor must not "
        "be scheduled yet, even though it is genuinely superseded"
    )
