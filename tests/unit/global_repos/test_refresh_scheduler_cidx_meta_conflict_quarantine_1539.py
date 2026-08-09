"""Unit tests for Bug #1539's persisted cidx-meta conflict quarantine.

Codex round-3 review rejected an earlier text-fingerprint design as
fundamentally fragile (both false-positive collisions and false-negative
misses) and as a one-way trap with no automatic recovery. This module
tests the SHA-based redesign against GoldenRepoMetadataSqliteBackend:
quarantine is keyed on the upstream target commit SHA being rebased onto
(resolve_upstream_target_sha), persisted so it is visible across separate
backend instances (simulating multiple workers/nodes sharing one SQLite
file), and automatically clears the moment that SHA changes -- no manual
reset required for the common case of new commits landing upstream.

The equivalent PostgreSQL-backend proof (real, live-PG-gated) lives in
test_golden_repo_metadata_cidx_meta_conflict_live_pg_1539.py. The two
real-git, un-mocked scheduler end-to-end proofs (auto-recovery on SHA
change; genuine skip on quarantine) live in
test_refresh_scheduler_cidx_meta_conflict_quarantine_e2e_1539.py.

Note on mocking scope: _make_scheduler_with_real_backend mocks several
RefreshScheduler methods belonging to the UNRELATED indexing/snapshot
pipeline (_detect_existing_indexes, _reconcile_registry_with_filesystem,
_index_source, _create_snapshot, swap_alias, is_write_locked,
_reset_fetch_failures, _has_local_changes) -- this is the SAME
established convention as the pre-existing
test_refresh_scheduler_cidx_meta_backup.py (Story #926), reused here
rather than duplicated a second time. Only the golden_repo_metadata
backend is real (a genuine SQLite file), since that IS the collaborator
under test.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
    RefreshScheduler,
)
from code_indexer.server.services.cidx_meta_backup.sync import (
    ConflictResolutionFailedError,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


# ---------------------------------------------------------------------------
# 1. Backend-level: target-SHA-aware conditional increment + reset + get
# ---------------------------------------------------------------------------


def _backend(tmp_path) -> GoldenRepoMetadataSqliteBackend:
    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
    backend.ensure_table_exists()
    return backend


def test_record_same_target_sha_increments(tmp_path):
    backend = _backend(tmp_path)
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
        == 1
    )
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d2")
        == 2
    )
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d3")
        == 3
    )


def test_record_different_target_sha_resets_to_one(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d2")
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-b", "d3")
        == 1
    )


def test_reset_clears_state(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
    backend.reset_cidx_meta_conflict_failure("cidx-meta-global")
    assert backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None


def test_get_state_reflects_last_target_sha_and_count(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "detail-1")
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "detail-2")
    state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
    assert state["consecutive_failure_count"] == 2
    assert state["last_target_sha"] == "sha-a"
    assert state["last_detail"] == "detail-2"


def test_reset_on_unknown_alias_is_a_noop(tmp_path):
    backend = _backend(tmp_path)
    backend.reset_cidx_meta_conflict_failure("never-seen-alias")  # must not raise


# ---------------------------------------------------------------------------
# 2. Cross-instance visibility -- the cluster-correctness proof. Two
#    SEPARATE backend instances opened against the SAME db path simulate
#    two different worker processes/nodes sharing storage.
# ---------------------------------------------------------------------------


def test_cross_instance_visibility(tmp_path):
    db_path = str(tmp_path / "shared_metadata.db")

    worker_a = GoldenRepoMetadataSqliteBackend(db_path)
    worker_a.ensure_table_exists()
    worker_a.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")
    worker_a.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d2")

    worker_b = GoldenRepoMetadataSqliteBackend(db_path)
    state_seen_by_worker_b = worker_b.get_cidx_meta_conflict_failure_state(
        "cidx-meta-global"
    )
    assert state_seen_by_worker_b["consecutive_failure_count"] == 2

    # A third failure recorded by worker B must be visible to worker A too.
    worker_b.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d3")
    state_seen_by_worker_a = worker_a.get_cidx_meta_conflict_failure_state(
        "cidx-meta-global"
    )
    assert state_seen_by_worker_a["consecutive_failure_count"] == 3


# ---------------------------------------------------------------------------
# 3. RefreshScheduler-level quarantine decision (SHA-aware, all mocked
#    apart from the real SQLite backend -- matches the pre-existing
#    test_refresh_scheduler_cidx_meta_backup.py mocking convention for
#    the unrelated indexing/snapshot pipeline).
# ---------------------------------------------------------------------------


def _make_scheduler_with_real_backend(tmp_path, backend=None):
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    cidx_meta_dir = golden_repos_dir / "cidx-meta"
    cidx_meta_dir.mkdir(parents=True)
    (cidx_meta_dir / ".code-indexer").mkdir()

    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")

    class _RegistryStub:
        def get_global_repo(self, alias_name):
            return {"repo_url": "local://cidx-meta", "default_branch": "master"}

        def update_refresh_timestamp(self, alias_name):
            return None

    if backend is None:
        backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
        backend.ensure_table_exists()

    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
        golden_repo_metadata_backend=backend,
    )
    sched.alias_manager.read_alias = MagicMock(
        return_value=str(golden_repos_dir / ".versioned" / "cidx-meta" / "v_1")
    )
    sched._detect_existing_indexes = MagicMock(return_value={})
    sched._reconcile_registry_with_filesystem = MagicMock()
    sched._index_source = MagicMock()
    sched._create_snapshot = MagicMock(return_value=str(tmp_path / "snapshot"))
    sched.alias_manager.swap_alias = MagicMock()
    sched.is_write_locked = MagicMock(return_value=False)
    sched._reset_fetch_failures = MagicMock()
    sched._has_local_changes = MagicMock(return_value=False)
    return sched, backend


def test_skip_result_is_none_when_target_sha_is_none(tmp_path):
    """Cannot determine quarantine without a resolved SHA -- must proceed."""
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d")
    assert (
        sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global", None)
        is None
    )


def test_skip_result_is_none_below_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "detail")
    assert (
        sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global", "sha-a")
        is None
    )


def test_skip_result_fires_at_threshold_for_same_sha(tmp_path):
    """(a) same SHA failing threshold times in a row quarantines."""
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "detail")
    skip_result = sched._cidx_meta_conflict_quarantine_skip_result(
        "cidx-meta-global", "sha-a"
    )
    assert skip_result is not None
    assert skip_result["skipped"] == "cidx_meta_conflict_quarantined"
    assert (
        skip_result["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD
    )


def test_skip_result_is_none_when_sha_differs_even_at_threshold(tmp_path):
    """(b) SHA changing after N failures does NOT quarantine -- the world
    changed (new upstream commits), so the stale count for the OLD SHA
    must not block a fresh attempt against the NEW SHA."""
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-old", "d")

    assert (
        sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global", "sha-new")
        is None
    )


def test_get_quarantine_state_if_active_below_at_above_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)

    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD - 1):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d")
    assert (
        sched._get_cidx_meta_conflict_quarantine_state_if_active(
            "cidx-meta-global", "sha-a"
        )
        is None
    )

    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d")
    state_at = sched._get_cidx_meta_conflict_quarantine_state_if_active(
        "cidx-meta-global", "sha-a"
    )
    assert state_at is not None
    assert (
        state_at["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD
    )

    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d")
    state_above = sched._get_cidx_meta_conflict_quarantine_state_if_active(
        "cidx-meta-global", "sha-a"
    )
    assert state_above is not None
    assert (
        state_above["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD + 1
    )


def test_perform_sync_records_failure_on_conflict_error(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    fake_sync = MagicMock()
    fake_sync.sync.side_effect = ConflictResolutionFailedError(
        "conflict resolution failed: boom",
        conflict_files=["shared.txt"],
        detail="boom",
    )

    with patch(
        "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
        return_value=fake_sync,
    ):
        with pytest.raises(ConflictResolutionFailedError):
            sched._perform_cidx_meta_backup_sync(
                "cidx-meta-global", "/fake/master/path", "master", "sha-a"
            )

    state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
    assert state["consecutive_failure_count"] == 1
    assert state["last_target_sha"] == "sha-a"


def test_perform_sync_resets_quarantine_on_success(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "sha-a", "d1")

    fake_sync = MagicMock()
    fake_sync.sync.return_value = SimpleNamespace(skipped=False, sync_failure=None)

    with patch(
        "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
        return_value=fake_sync,
    ):
        sched._perform_cidx_meta_backup_sync(
            "cidx-meta-global", "/fake/master/path", "master", "sha-a"
        )

    assert backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None


# ---------------------------------------------------------------------------
# 4. Cluster-mode-no-backend fails OPEN, never falls back to node-local
#    SQLite (Codex fail-closed/no-split-brain finding). golden_repo_
#    metadata_backend=None so the fast-path injected-backend short-circuit
#    is bypassed, forcing resolution through resolve_backend_registry_attr.
# ---------------------------------------------------------------------------


def _make_scheduler_no_injected_backend(tmp_path):
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    (golden_repos_dir / "cidx-meta").mkdir()

    class _RegistryStub:
        def get_global_repo(self, alias_name):
            return {"repo_url": "local://cidx-meta", "default_branch": "master"}

        def update_refresh_timestamp(self, alias_name):
            return None

    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
        golden_repo_metadata_backend=None,
    )


def test_cluster_mode_no_backend_resolves_to_none(tmp_path):
    sched = _make_scheduler_no_injected_backend(tmp_path)
    with patch(
        "code_indexer.server.utils.registry_factory.resolve_backend_registry_attr",
        return_value=(None, True),
    ):
        assert sched._resolve_cidx_meta_conflict_backend_or_none() is None


def test_cluster_mode_no_backend_skip_result_proceeds_not_quarantines(tmp_path):
    """(d) cluster-mode-no-backend-available must fail OPEN (proceed with
    sync) rather than silently falling back to a node-local SQLite
    backend that could split-brain against the real shared state."""
    sched = _make_scheduler_no_injected_backend(tmp_path)

    with patch(
        "code_indexer.server.utils.registry_factory.resolve_backend_registry_attr",
        return_value=(None, True),
    ):
        skip_result = sched._cidx_meta_conflict_quarantine_skip_result(
            "cidx-meta-global", "deadbeef"
        )
        assert skip_result is None

        # Recording/resetting must not raise even with no backend available.
        exc = ConflictResolutionFailedError(
            "conflict resolution failed: boom",
            conflict_files=["shared.txt"],
            detail="boom",
        )
        sched._record_cidx_meta_conflict_failure("cidx-meta-global", "deadbeef", exc)
        sched._reset_cidx_meta_conflict_quarantine("cidx-meta-global")
