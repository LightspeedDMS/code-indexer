"""Bug #1567: orphan sweep for leaked `.versioned` snapshots -- core
coverage (basic reap, current/previous protection, health gates,
missing/unreadable alias, no-ops, single-flight, unconditional deletion).

The sweep deletes unconditionally -- there is no "report" vs "delete"
mode. A bug fix must not ship behind an off-by-default toggle (the
config-mode wrapper this module previously had was removed; see
versioned_snapshot_reconciler.py's module docstring). The real safety
mechanisms (minimum-absolute-age floor, keep-last-N retention, pointer
protection, ts_live anchoring) are what decide WHICH paths are safe to
delete -- they are exercised throughout this file and are never gated by
a mode flag.

See versioned_snapshot_reconciler.py's module docstring for the full
Codex-hardened algorithm and rationale, and the sibling file
test_versioned_snapshot_reconciler_invariants_1567.py for the advanced
invariants ((a) cross-alias reference protection, (b) crash-orphan
exclusion, (c) corrupt/non-snapshot pointer preconditions,
(d) list_snapshots-raising isolation, (e) concurrent-swap interleaving,
(f) uncapped first-deploy backlog) that REPLACE the mass-deletion circuit
breaker the maintainer explicitly rejected.
"""

from __future__ import annotations

import os

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.job_tracker import DuplicateJobError
from code_indexer.server.services.versioned_snapshot_reconciler import (
    reconcile_versioned_snapshots,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)

# chmod-based permission denial is a no-op for a privileged (root) user.
_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_skip_if_root = pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="permission checks are bypassed for a root test runner"
)

#: keep_last=1 protects ONLY the live snapshot itself -- zero protection
#: from history.
KEEP_LAST_MINIMAL = 1

#: Mirrors snapshot_retention_keep_last's production default.
KEEP_LAST_PRODUCTION_DEFAULT = 3


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


def test_reaps_a_leaked_snapshot_that_was_never_scheduled(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LEAKED = 1000
    TS_LIVE = 2000
    leaked = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    assert leaked in cleanup_manager.get_pending_cleanups()
    assert leaked in result.scheduled_paths
    assert current not in cleanup_manager.get_pending_cleanups()


def test_never_schedules_current_or_previous_target(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    alias_name = f"{bare_ns}-global"
    TS_OLD_1 = 1000
    TS_OLD_2 = 2000
    TS_PREVIOUS = 3000
    TS_CURRENT = 4000
    old1 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_1)
    old2 = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD_2)
    previous = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_PREVIOUS)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_CURRENT)
    alias_manager.create_alias(alias_name, previous, repo_name=bare_ns)
    alias_manager.swap_alias(alias_name, new_target=current, old_target=previous)

    _result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    pending = cleanup_manager.get_pending_cleanups()
    assert current not in pending
    assert previous not in pending
    assert old1 in pending
    assert old2 in pending


@_skip_if_root
def test_health_gate_skips_sweep_when_versioned_base_dir_unreadable(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LEAKED = 1000
    TS_LIVE = 2000
    leaked = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    versioned_dir = golden_repos_dir / ".versioned"
    os.chmod(str(versioned_dir), 0o000)
    try:
        result = reconcile_versioned_snapshots(
            str(golden_repos_dir),
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
            retention_keep_last=KEEP_LAST_MINIMAL,
        )
    finally:
        os.chmod(str(versioned_dir), 0o755)

    assert result.aborted is True
    assert cleanup_manager.get_pending_cleanups() == set()
    assert leaked not in result.scheduled_paths


def test_missing_alias_yields_zero_deletions_for_that_repo(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "orphaned-repo-with-no-alias"
    TS_A, TS_B, TS_C = 1000, 2000, 3000
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_A)
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_B)
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_C)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    assert cleanup_manager.get_pending_cleanups() == set()
    assert result.scheduled_paths == []
    assert bare_ns in result.skipped_namespaces


@_skip_if_root
def test_genuinely_unreadable_alias_file_yields_zero_deletions_for_that_repo(
    tmp_path,
):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "unreadable-alias-repo"
    TS_OLD = 1000
    TS_LIVE = 2000
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_OLD)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)
    alias_file = golden_repos_dir / "aliases" / f"{bare_ns}-global.json"
    os.chmod(str(alias_file), 0o000)

    try:
        result = reconcile_versioned_snapshots(
            str(golden_repos_dir),
            snapshot_manager=snapshot_manager,
            alias_manager=alias_manager,
            cleanup_manager=cleanup_manager,
            retention_keep_last=KEEP_LAST_MINIMAL,
        )
    finally:
        os.chmod(str(alias_file), 0o644)

    assert cleanup_manager.get_pending_cleanups() == set()
    assert result.scheduled_paths == []
    assert bare_ns in result.skipped_namespaces


def test_single_flight_guard_skips_when_another_worker_already_running(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LEAKED = 1000
    TS_LIVE = 2000
    leaked = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    class _FakeConflictingJobTracker:
        def register_job_if_no_conflict(self, **kwargs):
            raise DuplicateJobError(
                operation_type=kwargs.get("operation_type", ""),
                repo_alias=kwargs.get("repo_alias"),
                existing_job_id="already-running",
            )

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        job_tracker=_FakeConflictingJobTracker(),
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    assert result.aborted is True
    assert leaked not in cleanup_manager.get_pending_cleanups()


def test_no_op_when_versioned_dir_does_not_exist_yet(tmp_path):
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_PRODUCTION_DEFAULT,
    )

    assert result.aborted is False
    assert result.scheduled_paths == []


def test_no_op_when_snapshot_manager_is_none(tmp_path):
    golden_repos_dir, alias_manager, _snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LEAKED = 1000
    _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=None,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
    )

    assert result.scheduled_paths == []
    assert cleanup_manager.get_pending_cleanups() == set()


def test_reconciler_deletes_unconditionally_by_default(tmp_path):
    """A bug fix must not ship behind an off-by-default toggle: calling
    reconcile_versioned_snapshots with NO mode-like argument at all must
    still schedule the deletion of a genuinely superseded snapshot
    through cleanup_manager -- the real (age-floor, keep-last,
    pointer-protection) safety guards are what gate WHICH paths are
    deleted, never a report/delete switch."""
    golden_repos_dir, alias_manager, snapshot_manager, cleanup_manager = _make_env(
        tmp_path
    )
    bare_ns = "myrepo"
    TS_LEAKED = 1000
    TS_LIVE = 2000
    leaked = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LEAKED)
    current = _make_snapshot_dir(golden_repos_dir, bare_ns, TS_LIVE)
    alias_manager.create_alias(f"{bare_ns}-global", current, repo_name=bare_ns)

    result = reconcile_versioned_snapshots(
        str(golden_repos_dir),
        snapshot_manager=snapshot_manager,
        alias_manager=alias_manager,
        cleanup_manager=cleanup_manager,
        retention_keep_last=KEEP_LAST_MINIMAL,
    )

    assert leaked in result.scheduled_paths
    assert leaked in cleanup_manager.get_pending_cleanups(), (
        "the reconciler must schedule deletion unconditionally -- there "
        "is no off-by-default mode gating this behavior"
    )
