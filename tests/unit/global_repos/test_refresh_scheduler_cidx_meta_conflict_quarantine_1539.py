"""Unit tests for Bug #1539's persisted cidx-meta conflict quarantine.

Covers the redesign that replaced an in-process, per-worker guard (which
Codex correctly identified as invisible across the multi-worker/multi-node
topology this server actually runs under) with a durable, dual-backend
counter -- mirroring Bug #1506's refresh-integrity quarantine mechanism
exactly, keyed by golden_alias via ``GoldenRepoMetadataSqliteBackend`` /
``GoldenRepoMetadataPostgresBackend``.
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
# 1. Backend-level: fingerprint-aware conditional increment + reset + get
# ---------------------------------------------------------------------------


def _backend(tmp_path) -> GoldenRepoMetadataSqliteBackend:
    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
    backend.ensure_table_exists()
    return backend


def test_record_same_fingerprint_increments(tmp_path):
    backend = _backend(tmp_path)
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d1") == 1
    )
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d2") == 2
    )
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d3") == 3
    )


def test_record_different_fingerprint_resets_to_one(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d1")
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d2")
    assert (
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-b", "d3") == 1
    )


def test_reset_clears_state(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d1")
    backend.reset_cidx_meta_conflict_failure("cidx-meta-global")
    assert backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None


def test_get_state_reflects_last_fingerprint_and_count(tmp_path):
    backend = _backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail-1")
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail-2")
    state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
    assert state["consecutive_failure_count"] == 2
    assert state["last_fingerprint"] == "fp-a"
    assert state["last_detail"] == "detail-2"


def test_reset_on_unknown_alias_is_a_noop(tmp_path):
    backend = _backend(tmp_path)
    backend.reset_cidx_meta_conflict_failure("never-seen-alias")  # must not raise


# ---------------------------------------------------------------------------
# 2. Cross-instance visibility -- the actual cluster-correctness proof.
#    Two SEPARATE backend instances opened against the SAME db path
#    simulate two different worker processes/nodes sharing storage.
# ---------------------------------------------------------------------------


def test_cross_instance_visibility(tmp_path):
    db_path = str(tmp_path / "shared_metadata.db")

    worker_a = GoldenRepoMetadataSqliteBackend(db_path)
    worker_a.ensure_table_exists()
    worker_a.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d1")
    worker_a.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d2")

    worker_b = GoldenRepoMetadataSqliteBackend(db_path)
    state_seen_by_worker_b = worker_b.get_cidx_meta_conflict_failure_state(
        "cidx-meta-global"
    )
    assert state_seen_by_worker_b["consecutive_failure_count"] == 2

    # A third failure recorded by worker B must be visible to worker A too.
    worker_b.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d3")
    state_seen_by_worker_a = worker_a.get_cidx_meta_conflict_failure_state(
        "cidx-meta-global"
    )
    assert state_seen_by_worker_a["consecutive_failure_count"] == 3


# ---------------------------------------------------------------------------
# 3. RefreshScheduler-level quarantine decision + skip wiring
# ---------------------------------------------------------------------------


def _make_scheduler_with_real_backend(tmp_path):
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


def test_skip_result_is_none_below_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    assert sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global") is None


def test_skip_result_fires_at_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    skip_result = sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global")
    assert skip_result is not None
    assert skip_result["skipped"] == "cidx_meta_conflict_quarantined"
    assert (
        skip_result["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD
    )


def test_skip_result_fires_above_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD + 2):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    skip_result = sched._cidx_meta_conflict_quarantine_skip_result("cidx-meta-global")
    assert skip_result is not None
    assert skip_result["skipped"] == "cidx_meta_conflict_quarantined"
    assert (
        skip_result["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD + 2
    )


def test_get_quarantine_state_if_active_below_at_above_threshold(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)

    # Below threshold: one failure short -> None (not yet active).
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD - 1):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    assert (
        sched._get_cidx_meta_conflict_quarantine_state_if_active("cidx-meta-global")
        is None
    )

    # At threshold: exactly one more failure -> active, count matches.
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    state_at = sched._get_cidx_meta_conflict_quarantine_state_if_active(
        "cidx-meta-global"
    )
    assert state_at is not None
    assert (
        state_at["consecutive_failure_count"]
        == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD
    )

    # Above threshold: one more failure still -> active, count keeps climbing.
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")
    state_above = sched._get_cidx_meta_conflict_quarantine_state_if_active(
        "cidx-meta-global"
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
                "cidx-meta-global", "/fake/master/path", "master"
            )

    state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
    assert state["consecutive_failure_count"] == 1


def test_perform_sync_resets_quarantine_on_success(tmp_path):
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "d1")

    fake_sync = MagicMock()
    fake_sync.sync.return_value = SimpleNamespace(skipped=False, sync_failure=None)

    with patch(
        "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
        return_value=fake_sync,
    ):
        sched._perform_cidx_meta_backup_sync(
            "cidx-meta-global", "/fake/master/path", "master"
        )

    assert backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None


def test_execute_refresh_skips_sync_when_quarantined(tmp_path):
    """The actual observable-behavior fix Codex demanded: once quarantined,
    ``_execute_refresh`` never even calls ``CidxMetaBackupSync.sync()`` --
    it returns a skip result immediately, so no more FAILED jobs pile up.
    """
    sched, backend = _make_scheduler_with_real_backend(tmp_path)
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", "fp-a", "detail")

    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url="file:///tmp/remote.git"
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )
    fake_sync = MagicMock()

    with (
        patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service",
            return_value=config_service,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler.CidxMetaBackupSync",
            return_value=fake_sync,
        ),
        patch("code_indexer.global_repos.refresh_scheduler.CidxMetaBackupBootstrap"),
        patch("code_indexer.global_repos.refresh_scheduler.MetaDirectoryUpdater"),
    ):
        result = sched._execute_refresh("cidx-meta-global")

    fake_sync.sync.assert_not_called()
    assert result["success"] is False
    assert result["skipped"] == "cidx_meta_conflict_quarantined"
