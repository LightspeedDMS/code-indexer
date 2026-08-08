"""Issue #1548 round-9 (ninth adversarial review round): a lost write lock
must abort EVERY destructive/mutating operation in ``mover.py``, not just
the shard publish/delete path round-8 already fixed.

Codex's round-9 reproduction: passing an already-lost lock signal, with an
orphaned staging directory present and a metadata scope pending, still let
BOTH the orphaned staging directory get deleted AND the metadata scope get
copied. Codex identified five unguarded mutation sites:

  1. orphaned staging deletion (``_cleanup_orphaned_staging_dirs``)
  2. orphaned trash deletion (``_cleanup_orphaned_trash_dirs``)
  3. metadata copying (``_copy_metadata_scope_if_safe``)
  4. relocation-record replacement (``_mark_repo_relocation_complete`` /
     ``_write_relocation_record_atomic``)
  5. private staging cleanup (``_publish``'s ``finally`` block)

...plus a structural "don't even attempt the next phase" requirement:
once ``_run_shard_pass`` observes lock loss, the caller must not proceed
into the metadata-scope sync phase at all.

Each test below exercises the REAL ``mover.py`` engine (no mocking of the
module under test) against a real filesystem, proving each site
individually discriminates on lock loss by observable, on-disk outcome.
Each destructive-site test is paired with a healthy-lock control proving
the refusal is a genuine lock-loss discrimination, not a pre-existing
unconditional no-op.
"""

import json
from pathlib import Path

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration import mover
from code_indexer.server.services.temporal_legacy_migration.locking import (
    LockLostError,
)
from code_indexer.server.services.temporal_legacy_migration.mover import (
    _STAGING_INFIX,
    _cleanup_orphaned_staging_dirs,
    _cleanup_orphaned_trash_dirs,
    _copy_metadata_scope_if_safe,
    _mark_repo_relocation_complete,
    _publish,
    migrate_temporal_shards,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.temporal_metadata_sqlite_backend import (
    TemporalMetadataSqliteBackend,
)


def _write_real_hnsw_index(shard_dir: Path, point_id: str, vector: list) -> None:
    manager = HNSWIndexManager(vector_dim=len(vector), space="cosine")
    manager.build_index(shard_dir, np.array([vector], dtype=np.float32), [point_id])


def _write_complete_shard(shard_dir: Path, point_id: str) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(
        json.dumps({"id": point_id, "vector": [1.0]})
    )
    (shard_dir / "collection_meta.json").write_text('{"name":"q1"}')
    _write_real_hnsw_index(shard_dir, point_id, [1.0])


def _populate_metadata_scope(scope_path: Path) -> TemporalMetadataSqliteBackend:
    """Create a real, non-empty temporal-metadata scope at *scope_path*
    via the real SQLite backend -- the shared setup every metadata-scope
    test in this module reuses.
    """
    backend = TemporalMetadataSqliteBackend(scope_path)
    backend.save_metadata("point-1", {"commit_hash": "abc", "path": "f.py"})
    return backend


def _write_orphaned_staging_dir(fixed_root: Path) -> Path:
    """Create an orphaned staging directory at *fixed_root*, as if a
    prior pass crashed between ``copytree`` and the atomic rename -- the
    shared setup every orphaned-staging test in this module reuses.
    """
    orphan = fixed_root / f".code-indexer-temporal-e-2026Q1{_STAGING_INFIX}deadbeef"
    orphan.mkdir(parents=True)
    (orphan / "marker.txt").write_text("scratch data")
    return orphan


def _write_matching_trash_and_target(
    legacy_root: Path, fixed_root: Path, shard_name: str, point_id: str
) -> Path:
    """Create a trash directory (as if orphaned by a crash between the
    atomic rename in ``_delete_source_atomically`` and its subsequent
    ``shutil.rmtree``) alongside a fixed-root target that ``_trash_dir_
    is_safe_to_discard`` would positively confirm safe to discard -- the
    shared setup every orphaned-trash test in this module reuses. Returns
    the trash directory path.
    """
    trash = legacy_root / f".{shard_name}.pending-delete-deadbeef"
    _write_complete_shard(trash, point_id)
    target = fixed_root / shard_name
    _write_complete_shard(target, point_id)
    (target / mover._PROVENANCE_MARKER_NAME).write_text(mover.manifest_digest(target))
    return trash


class _FakeLockLossSignal:
    """Same structural fake used by ``test_mover_lock_lost_1548.py``: NOT
    lost for the first *calls_before_loss* checks, then permanently lost.
    """

    def __init__(self, calls_before_loss: int) -> None:
        self._remaining = calls_before_loss
        self._lost = False

    def _check(self) -> bool:
        if not self._lost:
            if self._remaining <= 0:
                self._lost = True
            else:
                self._remaining -= 1
        return self._lost

    def is_lost(self) -> bool:
        return self._check()

    def raise_if_lost(self) -> None:
        if self._check():
            raise LockLostError("lock lost (test fake)")


class _AlwaysLost:
    """Permanently-lost fake -- simplest possible signal for tests that
    only need "already lost from the very first check".
    """

    def is_lost(self) -> bool:
        return True

    def raise_if_lost(self) -> None:
        raise LockLostError("lock lost (test fake, always)")


# ---------------------------------------------------------------------------
# Site 1: orphaned staging deletion
# ---------------------------------------------------------------------------


def test_orphaned_staging_dir_not_deleted_when_lock_already_lost(tmp_path: Path):
    fixed_root = tmp_path / "fixed"
    orphan = _write_orphaned_staging_dir(fixed_root)

    failures = _cleanup_orphaned_staging_dirs(fixed_root, lock_lost_check=_AlwaysLost())

    assert failures == 0, "a deferred orphan is not a failure"
    assert orphan.exists(), "orphaned staging dir must survive when lock is lost"


def test_orphaned_staging_dir_deleted_when_lock_not_lost(tmp_path: Path):
    """Control: without a lock-lost signal, the pre-existing sweep
    behavior is unchanged -- proves the guard above discriminates rather
    than always refusing.
    """
    fixed_root = tmp_path / "fixed"
    orphan = _write_orphaned_staging_dir(fixed_root)

    failures = _cleanup_orphaned_staging_dirs(fixed_root, lock_lost_check=None)

    assert failures == 0
    assert not orphan.exists(), "orphan must be swept when there is no lock concern"


# ---------------------------------------------------------------------------
# Site 2: orphaned trash deletion
# ---------------------------------------------------------------------------


def test_orphaned_trash_dir_not_deleted_when_lock_already_lost(tmp_path: Path):
    legacy_root = tmp_path / "legacy"
    fixed_root = tmp_path / "fixed"
    legacy_root.mkdir(parents=True)
    # A trash dir that WOULD be positively confirmed safe to discard if
    # the lock were not lost -- this proves the lock check, not the
    # safety check, is what is under test here.
    trash = _write_matching_trash_and_target(
        legacy_root, fixed_root, "code-indexer-temporal-e-2026Q1", "p1"
    )

    failures = _cleanup_orphaned_trash_dirs(
        legacy_root, fixed_root, lock_lost_check=_AlwaysLost()
    )

    assert failures == 0
    assert trash.exists(), "orphaned trash dir must survive when lock is lost"


def test_orphaned_trash_dir_deleted_when_lock_not_lost(tmp_path: Path):
    """Control: identical setup, no lock-lost signal -- proves the guard
    above discriminates rather than always refusing (the trash dir is
    positively confirmed safe to discard and is genuinely removed).
    """
    legacy_root = tmp_path / "legacy"
    fixed_root = tmp_path / "fixed"
    legacy_root.mkdir(parents=True)
    trash = _write_matching_trash_and_target(
        legacy_root, fixed_root, "code-indexer-temporal-e-2026Q1", "p1"
    )

    failures = _cleanup_orphaned_trash_dirs(
        legacy_root, fixed_root, lock_lost_check=None
    )

    assert failures == 0
    assert not trash.exists(), "trash must be swept when there is no lock concern"


# ---------------------------------------------------------------------------
# Site 3: metadata copying
# ---------------------------------------------------------------------------


def test_metadata_scope_copy_refused_when_lock_lost(tmp_path: Path):
    legacy_meta = tmp_path / "legacy" / LEGACY_TEMPORAL_COLLECTION
    fixed_meta = tmp_path / "fixed" / LEGACY_TEMPORAL_COLLECTION
    _populate_metadata_scope(legacy_meta)

    copy_failed = _copy_metadata_scope_if_safe(
        legacy_meta,
        fixed_meta,
        TemporalMetadataSqliteBackend,
        relocation_enabled=True,
        withhold=False,
        lock_lost_check=_AlwaysLost(),
    )

    assert copy_failed is False, "a refused copy is not a failure"
    assert not (fixed_meta / "temporal_metadata.db").exists(), (
        "metadata scope must never be copied once the lock may have been lost"
    )


def test_metadata_scope_copy_proceeds_when_lock_healthy(tmp_path: Path):
    legacy_meta = tmp_path / "legacy" / LEGACY_TEMPORAL_COLLECTION
    fixed_meta = tmp_path / "fixed" / LEGACY_TEMPORAL_COLLECTION
    _populate_metadata_scope(legacy_meta)

    copy_failed = _copy_metadata_scope_if_safe(
        legacy_meta,
        fixed_meta,
        TemporalMetadataSqliteBackend,
        relocation_enabled=True,
        withhold=False,
        lock_lost_check=None,
    )

    assert copy_failed is False
    assert (fixed_meta / "temporal_metadata.db").exists()


# ---------------------------------------------------------------------------
# Site 4: relocation-record replacement
# ---------------------------------------------------------------------------


def test_relocation_record_not_written_when_lock_lost(tmp_path: Path):
    fixed_root = tmp_path / "fixed"
    legacy_root = tmp_path / "legacy" / ".code-indexer" / "index"
    fixed_root.mkdir(parents=True)

    _mark_repo_relocation_complete(
        fixed_root,
        legacy_root,
        {"code-indexer-temporal-e-2026Q1": "a" * 64},
        lock_lost_check=_AlwaysLost(),
    )

    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert not marker.exists(), (
        "repo-level relocation record must never be written once the "
        "lock may have been lost"
    )


def test_relocation_record_written_when_lock_healthy(tmp_path: Path):
    fixed_root = tmp_path / "fixed"
    legacy_root = tmp_path / "legacy" / ".code-indexer" / "index"
    fixed_root.mkdir(parents=True)

    _mark_repo_relocation_complete(
        fixed_root,
        legacy_root,
        {"code-indexer-temporal-e-2026Q1": "a" * 64},
        lock_lost_check=None,
    )

    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert marker.exists()


# ---------------------------------------------------------------------------
# Site 5: private staging cleanup in _publish's finally block
# ---------------------------------------------------------------------------


def test_publish_staging_scratch_left_in_place_when_lock_lost_before_cleanup(
    tmp_path: Path,
):
    """Force the abort to fire INSIDE _publish (immediately before the
    rename), then confirm the staging scratch directory it created is
    left on disk (deferred to a later orphan sweep) rather than removed
    in the ``finally`` block -- proving the finally-block guard, not just
    the raise itself. With an always-lost signal, the abort fires at
    ``_abort_if_lock_lost`` before the rename, so ``staging`` still
    exists when the ``finally`` block runs and its own guard is what is
    under test here.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    source = legacy / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(source, "p1")
    target = tmp_path / ".temporal" / "repo" / "code-indexer-temporal-e-2026Q1"

    with pytest.raises(LockLostError):
        _publish(source, target, pre_publish_hook=None, lock_lost_check=_AlwaysLost())

    assert not target.exists(), "publish must never have completed"
    staging_dirs = list(target.parent.glob(f".{target.name}{_STAGING_INFIX}*"))
    assert len(staging_dirs) == 1, (
        "the staging scratch directory _publish created must survive -- "
        "left for a later orphan sweep instead of being deleted while the "
        "lock may have been lost"
    )


def test_publish_staging_scratch_removed_normally_on_success(tmp_path: Path):
    """Control: with no lock-lost signal, publish completes and its own
    scratch directory is renamed away (not merely left in place) -- the
    happy path is unaffected by the round-9 guard.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    source = legacy / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(source, "p1")
    target = tmp_path / ".temporal" / "repo" / "code-indexer-temporal-e-2026Q1"

    _publish(source, target, pre_publish_hook=None, lock_lost_check=None)

    assert target.exists()
    assert not list(target.parent.glob(f".{target.name}{_STAGING_INFIX}*"))


# ---------------------------------------------------------------------------
# Structural requirement: don't proceed into metadata sync after
# _run_shard_pass observes lock loss.
# ---------------------------------------------------------------------------


def test_engine_does_not_proceed_into_metadata_sync_after_shard_pass_lock_loss(
    tmp_path: Path,
):
    """Observable-outcome proof (no mocking of the module under test):
    a shard genuinely publishes, then the NEW top-level structural check
    in ``migrate_temporal_shards`` (made immediately after
    ``_run_shard_pass`` returns) reports the lock as lost. A real,
    non-empty legacy metadata scope is present, so if the engine
    proceeded into the metadata-sync phase regardless, the metadata scope
    would be copied and/or the relocation record would be written.
    Neither happens.

    Exact lock-check sequence for this one-shard, relocation-only
    (``cleanup_authorized=False``) pass, so ``calls_before_loss=2`` lands
    the loss precisely at the new top-level check: call #1 is
    ``_run_shard_pass``'s own top-of-loop check; call #2 is ``_publish``'s
    internal ``_abort_if_lock_lost`` immediately before its rename. Since
    the publish SUCCEEDS, ``staging`` no longer exists by the time
    ``_publish``'s ``finally`` block runs, so its own lock check (site 5,
    guarded by ``if staging.exists():``) is never reached on this
    (happy) path and contributes no additional call. Call #3 is therefore
    the new top-level check below, immediately after ``_run_shard_pass``
    returns -- exactly where ``calls_before_loss=2`` reports loss.
    """
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(shard, "p1")
    _populate_metadata_scope(legacy / LEGACY_TEMPORAL_COLLECTION)

    result = migrate_temporal_shards(
        legacy,
        fixed,
        relocation_enabled=True,
        cleanup_authorized=False,
        metadata_backend_factory=TemporalMetadataSqliteBackend,
        lock_lost_check=_FakeLockLossSignal(calls_before_loss=2),
    )

    # The shard publish itself (non-destructive addition) DID complete --
    # proving the abort is scoped to the metadata phase, not a rollback of
    # already-completed non-destructive work.
    assert (fixed / shard.name / "collection_meta.json").exists()
    assert result.published == 1
    assert result.failed == 0
    # The metadata-sync phase was never entered: neither the copy nor the
    # relocation-record write happened, even though a real, non-empty
    # legacy metadata scope was present.
    fixed_meta = fixed / LEGACY_TEMPORAL_COLLECTION
    assert not (fixed_meta / "temporal_metadata.db").exists()
    marker = fixed / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Codex's exact reproduction scenario, reproduced end to end through the
# real public engine entry point.
# ---------------------------------------------------------------------------


def test_codex_exact_scenario_staging_and_metadata_both_refused(tmp_path: Path):
    """Lock already lost, an orphaned staging directory present, and a
    metadata scope pending -- BOTH the orphaned staging deletion AND the
    metadata copy must be refused. Zero temporal shards exist so the
    scenario isolates exactly the two operations Codex's repro named.
    """
    legacy_root = tmp_path / "repo" / ".code-indexer" / "index"
    fixed_root = tmp_path / ".temporal" / "repo"
    legacy_root.mkdir(parents=True)
    orphan = _write_orphaned_staging_dir(fixed_root)
    _populate_metadata_scope(legacy_root / LEGACY_TEMPORAL_COLLECTION)

    result = migrate_temporal_shards(
        legacy_root,
        fixed_root,
        relocation_enabled=True,
        cleanup_authorized=True,
        metadata_backend_factory=TemporalMetadataSqliteBackend,
        lock_lost_check=_AlwaysLost(),
    )

    # Both destructive/mutating operations Codex's repro named are refused.
    assert orphan.exists(), "orphaned staging directory must survive"
    fixed_meta = fixed_root / LEGACY_TEMPORAL_COLLECTION
    assert not (fixed_meta / "temporal_metadata.db").exists(), (
        "metadata scope must never be copied"
    )
    assert result.published == 0
    assert result.deleted == 0


def test_codex_exact_scenario_both_operations_succeed_when_lock_healthy(
    tmp_path: Path,
):
    """Control for the scenario above: with a healthy lock (no signal at
    all), the same starting state results in BOTH the orphan being swept
    AND the metadata scope being copied -- proving the refusal above is a
    genuine lock-loss discrimination, not a pre-existing unconditional
    no-op.
    """
    legacy_root = tmp_path / "repo" / ".code-indexer" / "index"
    fixed_root = tmp_path / ".temporal" / "repo"
    legacy_root.mkdir(parents=True)
    orphan = _write_orphaned_staging_dir(fixed_root)
    _populate_metadata_scope(legacy_root / LEGACY_TEMPORAL_COLLECTION)

    migrate_temporal_shards(
        legacy_root,
        fixed_root,
        relocation_enabled=True,
        cleanup_authorized=True,
        metadata_backend_factory=TemporalMetadataSqliteBackend,
        lock_lost_check=None,
    )

    assert not orphan.exists()
    fixed_meta = fixed_root / LEGACY_TEMPORAL_COLLECTION
    assert (fixed_meta / "temporal_metadata.db").exists()
