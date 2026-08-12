"""Bug #1567: orphan sweep for leaked `.versioned` snapshots -- advanced
invariant coverage. These are the properties that REPLACE the
mass-deletion circuit breaker the maintainer explicitly rejected:

  (a) no scheduled path equals any pointer's target_path/previous_path,
      across EVERY alias file in the aliases directory.
  (b) no scheduled path has ts >= ts_live -- the crash-orphan / in-flight
      publish exclusion, THE round-2 fix for a live-data-deleting hole.
  (c) a missing, corrupt, or non-snapshot-shaped pointer schedules
      exactly zero deletions for that namespace.
  (d) a namespace whose snapshot directory cannot be listed schedules
      exactly zero deletions.
  (e) a sweep interleaved with a concurrent swap_alias, in EITHER
      ordering, never schedules the genuinely live target.
  (f) first-deploy: a large backlog of genuinely superseded snapshots is
      ALL scheduled (except the small history-retention margin) in ONE
      sweep -- no cap, no multi-pass requirement.

See the sibling file test_versioned_snapshot_reconciler_1567.py for the
core/basic coverage, and versioned_snapshot_reconciler.py's module
docstring for the full algorithm and rationale.
"""

from __future__ import annotations

import json
import os

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.versioned_snapshot_reconciler import (
    compute_snapshot_deletion_candidates,
    read_pointer_target_and_previous,
    reconcile_versioned_snapshots,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

#: keep_last=1 protects ONLY the live snapshot itself -- zero protection
#: from history -- used to isolate a DIFFERENT protection mechanism
#: (ts>=ts_live exclusion, cross-alias reference union) from incidental
#: "newest N of history" retention.
KEEP_LAST_MINIMAL = 1

#: Mirrors snapshot_retention_keep_last's production default.
KEEP_LAST_PRODUCTION_DEFAULT = 3

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


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


def _write_pointer_raw(aliases_dir, alias_name: str, payload) -> None:
    """Direct JSON write -- used where a test needs precise control
    (malformed payload, non-snapshot target) that AliasManager's own API
    does not expose."""
    aliases_dir.mkdir(parents=True, exist_ok=True)
    (aliases_dir / f"{alias_name}.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload)
    )


def test_corrupt_json_pointer_yields_zero_deletions_for_that_repo(tmp_path):
    """Invariant (c), part 1: malformed JSON must never be interpreted --
    skip entirely, never guess."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "corrupt-pointer-repo"
    TS_OLD = 1000
    TS_LIVE = 2000
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    _write_pointer_raw(
        golden_repos_dir / "aliases", f"{bare_ns}-global", "{not valid json"
    )

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        mode="delete",
    )

    assert result.scheduled_paths == []
    assert current not in cleanup_manager.get_pending_cleanups()
    assert bare_ns in result.skipped_namespaces


def test_non_snapshot_target_path_yields_zero_deletions_for_that_repo(tmp_path):
    """Invariant (c), part 2: target_path that is not a v_<ts> snapshot
    literally under this namespace (e.g. the master clone on first
    refresh) must never be interpreted -- skip the whole namespace."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "master-clone-repo"
    TS_OLD = 1000
    old = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD)
    master_clone_path = str(golden_repos_dir / bare_ns)
    alias_manager.create_alias(
        f"{bare_ns}-global", master_clone_path, repo_name=bare_ns
    )

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        mode="delete",
    )

    assert old not in cleanup_manager.get_pending_cleanups()
    assert result.scheduled_paths == []
    assert bare_ns in result.skipped_namespaces


def test_list_snapshots_raising_yields_zero_deletions_for_that_repo(tmp_path):
    """Invariant (d): a namespace directory that cannot be listed must
    schedule exactly zero -- never guessed, never treated as empty."""
    if _RUNNING_AS_ROOT:
        import pytest

        pytest.skip("permission checks are bypassed for a root test runner")

    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "unlistable-repo"
    TS_OLD = 1000
    TS_LIVE = 2000
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    ns_dir = golden_repos_dir / ".versioned" / bare_ns
    os.chmod(str(ns_dir), 0o000)
    try:
        result = reconcile_versioned_snapshots(
            str(golden_repos_dir),
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
            retention_keep_last=KEEP_LAST_MINIMAL,
            mode="delete",
        )
    finally:
        os.chmod(str(ns_dir), 0o755)

    assert result.scheduled_paths == []
    assert cleanup_manager.get_pending_cleanups() == set()
    assert bare_ns in result.skipped_namespaces


def test_crash_orphan_newer_than_live_target_is_never_scheduled(tmp_path):
    """Invariant (b) -- THE round-2 fix. A snapshot NEWER than the live
    target (a crash-orphan, or an in-flight publish not yet swapped in)
    must NEVER be scheduled, regardless of keep_last. Closes round 1's
    live-data-deleting hole."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LIVE = 2000
    TS_ORPHAN_1 = 2100
    TS_ORPHAN_2 = 2200
    TS_ORPHAN_3 = 2300
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)
    orphan1 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_ORPHAN_1)
    orphan2 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_ORPHAN_2)
    orphan3 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_ORPHAN_3)

    # KEEP_LAST_MINIMAL: zero protection from history, proving the
    # ts>=ts_live exclusion (not incidental history retention) protects
    # the orphans.
    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        mode="delete",
    )

    pending = cleanup_manager.get_pending_cleanups()
    for orphan in (orphan1, orphan2, orphan3, current):
        assert orphan not in pending
        assert orphan not in result.scheduled_paths


def test_referenced_by_any_pointer_across_all_alias_files_is_protected(tmp_path):
    """Invariant (a): a snapshot referenced by ANY alias pointer file,
    not just this namespace's own governing one, must be protected --
    covers retired-but-still-present temporal sister pointers
    (Bug #1528/#1529)."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_OLD_1 = 100
    TS_REFERENCED_BY_OTHER_ALIAS = 200
    TS_OLD_3 = 300
    TS_LIVE = 500
    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_1)
    referenced_by_other_alias = _make_snapshot_dir(
        golden_repos_dir, bare_ns, TS_REFERENCED_BY_OTHER_ALIAS
    )
    old3 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_3)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)
    # A SEPARATE, unrelated alias file (e.g. a retired temporal sister
    # pointer) references the mid-range snapshot within THIS namespace.
    _write_pointer_raw(
        golden_repos_dir / "aliases",
        f"{bare_ns}-temporal-embedv4",
        {"target_path": referenced_by_other_alias},
    )

    _result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        mode="delete",
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert referenced_by_other_alias not in pending
    assert old1 in pending
    assert old3 in pending
    assert current not in pending


def test_first_deploy_backlog_scheduled_in_one_sweep_no_cap(tmp_path):
    """Invariant (f): the real production condition this fix targets --
    NO ratio guard, NO multi-pass requirement. A 225-snapshot backlog,
    keep_last=3 (2 protected from history plus the live target itself =
    3 total kept), so 223 must be scheduled in ONE sweep."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "langfuse"
    BACKLOG_SIZE = 225  # matches the live incident's measured 229-per-repo scale
    TS_LIVE = 9999
    superseded = [
        _make_snapshot_dir(golden_repos_dir, bare_ns, ts)
        for ts in range(1, BACKLOG_SIZE + 1)
    ]
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_PRODUCTION_DEFAULT,
        mode="delete",
    )

    pending = cleanup_manager.get_pending_cleanups()
    protected_from_history_count = KEEP_LAST_PRODUCTION_DEFAULT - 1  # == 2
    kept_from_history = set(superseded[-protected_from_history_count:])
    for path in superseded:
        if path in kept_from_history:
            assert path not in pending
        else:
            assert path in pending, f"{path} should have been scheduled"
    assert current not in pending
    assert len(result.scheduled_paths) == BACKLOG_SIZE - protected_from_history_count


def test_concurrent_swap_stale_read_before_swap_never_schedules_new_live_target(
    tmp_path,
):
    """Invariant (e), ordering 1: THE pointer file is genuinely READ
    (via read_pointer_target_and_previous, the same primitive the sweep
    itself uses) BEFORE swap_alias() runs. Only AFTER that real read
    does the swap complete, publishing a new live target the reader
    never saw. The stale target_path captured from that read must still
    never cause the genuinely new live target to be scheduled when it is
    fed into cleanup_manager.schedule_cleanup() -- the same call
    reconcile_versioned_snapshots() uses internally -- proving safety-
    directional staleness end to end."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"
    TS_OLD_1 = 100
    TS_OLD_2 = 200
    TS_LIVE_BEFORE_SWAP = 300
    TS_LIVE_AFTER_SWAP = 400

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_1)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_2)
    target_before_swap = _make_snapshot_dir(
        golden_repos_dir, bare_ns, TS_LIVE_BEFORE_SWAP
    )
    alias_manager.create_alias(alias_name, target_before_swap, repo_name=bare_ns)

    # Genuinely READ the pointer file BEFORE the swap happens.
    alias_file = golden_repos_dir / "aliases" / f"{alias_name}.json"
    stale_read = read_pointer_target_and_previous(alias_file)
    assert stale_read is not None
    stale_target_path, _stale_previous_path = stale_read

    # NOW the swap happens, publishing a target the stale read never saw.
    target_after_swap = _make_snapshot_dir(
        golden_repos_dir, bare_ns, TS_LIVE_AFTER_SWAP
    )
    alias_manager.swap_alias(
        alias_name, new_target=target_after_swap, old_target=target_before_swap
    )

    candidates = compute_snapshot_deletion_candidates(
        bare_ns,
        golden_repos_dir=golden_repos_dir,
        snapshots=snapshot_manager.list_snapshots(bare_ns),
        target_path=stale_target_path,  # the STALE, pre-swap read
        referenced_paths={stale_target_path},
        keep_last=KEEP_LAST_MINIMAL,
        min_absolute_age_seconds=0.0,
    )
    for path in candidates:
        cleanup_manager.schedule_cleanup(path)

    pending = cleanup_manager.get_pending_cleanups()
    assert target_after_swap not in pending, (
        "a genuinely stale (pre-swap) pointer read must never cause the "
        "post-swap live target to be scheduled for deletion"
    )
    assert target_before_swap not in pending  # still live-or-newer to the stale read
    assert old1 in pending
    assert old2 in pending


def test_concurrent_swap_fresh_read_after_swap_protects_both_targets(tmp_path):
    """Invariant (e), ordering 2: the swap has fully completed BEFORE
    the sweep runs, so a FRESH read sees the CURRENT pointer -- both the
    new target and the previous target must be protected, and the
    genuinely old snapshots remain candidates."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"
    TS_OLD_1 = 100
    TS_OLD_2 = 200
    TS_LIVE_BEFORE_SWAP = 300
    TS_LIVE_AFTER_SWAP = 400

    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_1)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_2)
    target_before_swap = _make_snapshot_dir(
        golden_repos_dir, bare_ns, TS_LIVE_BEFORE_SWAP
    )
    alias_manager.create_alias(alias_name, target_before_swap, repo_name=bare_ns)
    target_after_swap = _make_snapshot_dir(
        golden_repos_dir, bare_ns, TS_LIVE_AFTER_SWAP
    )
    alias_manager.swap_alias(
        alias_name, new_target=target_after_swap, old_target=target_before_swap
    )

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
        mode="delete",
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert target_after_swap not in pending
    assert target_before_swap not in pending
    assert old1 in pending
    assert old2 in pending
    assert result.aborted is False
