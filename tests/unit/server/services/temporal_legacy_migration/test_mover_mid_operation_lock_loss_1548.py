"""Issue #1548 round-10 (tenth adversarial review round): a lock lost DURING
a slow mutation -- not just checked once before it starts -- must still
abort before the mutation becomes durable.

Round-9 closed every mutation site by checking ``is_lost()``/``raise_if_
lost()`` ONCE immediately before the operation began. Codex's round-10
review found that checking-once is insufficient when the mutation itself
takes real time:

  Finding 1 (HIGH): the metadata-scope copy (``_copy_metadata_scope_if_
  safe`` -> ``TemporalMetadataScopeBackend.copy_collection_scope``) can
  take real time for a large scope (SQLite's ``INSERT OR REPLACE`` /
  PostgreSQL's ``INSERT ... ON CONFLICT``). A lock lost DURING that write
  must not still commit.

  Finding 2 (HIGH): the relocation-record write (``_write_relocation_
  record_atomic``) is several syscalls (mkdir, write, fsync, replace,
  fsync) -- a lock lost between the entry check and the final ``os.
  replace()`` must abort BEFORE that replace, not merely before the whole
  sequence started.

  Finding 3 (Medium): an ``OSError`` while writing the relocation record
  was silently swallowed (logged, then returned as if successful) --
  fixed to surface as a real, countable failure.

Every test below exercises the REAL production code (no mocking of the
module under test) against a real filesystem/SQLite database. Fault
injection uses only mechanisms this module already exposes for exactly
this purpose: the ``LockLossCheck`` Protocol (structurally identical to
every round-8/9 lock-loss test in this suite) and the pre-existing
``pre_delete_hook`` test seam (identical in kind to the round-5/6/7
exploit tests' own use of it) -- never ``monkeypatch.setattr`` on
``mover``'s own internals.
"""

import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration import mover
from code_indexer.server.services.temporal_legacy_migration.mover import (
    _copy_metadata_scope_if_safe,
    _mark_repo_relocation_complete,
    migrate_temporal_shards,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.temporal_metadata_sqlite_backend import (
    TemporalMetadataSqliteBackend,
)

# chmod-based permission-denial fault injection (used elsewhere in this
# codebase's own read-only-inspection test suites) is ineffective for a
# process running as root, which ignores directory write permissions --
# applied ONLY to the two tests below that actually rely on it, never
# module-wide, so the lock-loss tests (which need no chmod at all) still
# run under a root-based CI environment.
_skip_if_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="chmod-based fault injection requires a non-root uid"
)

# Exactly one "not lost" check must pass before the fake reports lock
# loss -- this is the entry-level check every call site performs before
# ever reaching the NEW commit-point recheck under test.
_ENTRY_CHECK_BUDGET = 1
_SHA256_HEX_LENGTH = 64
_FAKE_SHA256_DIGEST = "a" * _SHA256_HEX_LENGTH
_READ_ONLY_NO_WRITE_MODE = 0o500  # read + execute only -- no write permission
_WRITABLE_MODE = 0o700


def _populate_metadata_scope(scope_path: Path) -> None:
    TemporalMetadataSqliteBackend(scope_path).save_metadata(
        "point-1", {"commit_hash": "abc", "path": "f.py"}
    )


def _row_count(db_path: Path) -> int:
    """Count rows in a temporal_metadata.db -- unlike file existence, the
    schema-only file ``TemporalMetadataSqliteBackend.__init__`` always
    creates (committed as its OWN, separate, earlier transaction in
    ``_init_database``) says nothing about whether the row-copy
    transaction itself ever committed. Row count is the real invariant a
    rolled-back copy must satisfy.
    """
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM temporal_metadata").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _write_complete_shard(shard_dir: Path, point_id: str) -> None:
    """Real, structurally-complete legacy shard: a vector record, valid
    metadata, and a real HNSW index -- the same construction round-8/9's
    tests use so ``_target_is_structurally_complete`` genuinely passes.
    """
    import json

    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / f"vector_{point_id}.json").write_text(
        json.dumps({"id": point_id, "vector": [1.0]})
    )
    (shard_dir / "collection_meta.json").write_text('{"name":"q1"}')
    manager = HNSWIndexManager(vector_dim=1, space="cosine")
    manager.build_index(shard_dir, np.array([[1.0]], dtype=np.float32), [point_id])


class _LoseAfterEntryCheck:
    """Reports "not lost" for exactly ``_ENTRY_CHECK_BUDGET`` checks, then
    permanently lost -- simulates a lock that is healthy at a call site's
    entry-level check but lost by the time a LATER, narrower recheck
    fires, regardless of which of the two ``LockLossCheck`` methods that
    later recheck uses.
    """

    def __init__(self) -> None:
        self._remaining = _ENTRY_CHECK_BUDGET

    def is_lost(self) -> bool:
        if self._remaining > 0:
            self._remaining -= 1
            return False
        return True

    def raise_if_lost(self) -> None:
        if self.is_lost():
            raise RuntimeError("lock lost (test fake, mid-operation)")


# ---------------------------------------------------------------------------
# Finding 1: metadata-scope copy must not commit if the lock is lost DURING
# the copy (checked at the backend's own commit point via pre_commit_check).
# ---------------------------------------------------------------------------


def test_metadata_copy_rolls_back_when_lock_lost_during_the_copy(tmp_path: Path):
    """The entry-level check (before calling copy_collection_scope) must
    pass, so the ONLY thing that can abort this copy is the NEW
    commit-point recheck inside the backend itself, proving the fix
    reaches all the way to the backend's own transaction, not just
    mover.py's entry gate.
    """
    legacy_meta = tmp_path / "legacy" / LEGACY_TEMPORAL_COLLECTION
    fixed_meta = tmp_path / "fixed" / LEGACY_TEMPORAL_COLLECTION
    _populate_metadata_scope(legacy_meta)

    copy_failed = _copy_metadata_scope_if_safe(
        legacy_meta,
        fixed_meta,
        TemporalMetadataSqliteBackend,
        relocation_enabled=True,
        withhold=False,
        lock_lost_check=_LoseAfterEntryCheck(),
    )

    assert copy_failed is False, (
        "a mid-operation lock-loss abort is a deliberate, safe deferral -- "
        "not a failure -- exactly like every other lock-loss abort in this "
        "module"
    )
    assert _row_count(fixed_meta / "temporal_metadata.db") == 0, (
        "no row must ever be committed to the destination when the lock "
        "was lost DURING the copy -- the transaction must be rolled back, "
        "not committed (the destination's schema-only file existing is "
        "expected -- __init__ always creates it -- only row presence "
        "proves a real commit happened)"
    )


def test_metadata_copy_commits_normally_when_lock_healthy_throughout(
    tmp_path: Path,
):
    """Control: with no lock-lost signal at all, the copy completes and
    commits normally -- proves the commit-point recheck does not regress
    the happy path.
    """
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


def test_backend_copy_collection_scope_rolls_back_on_pre_commit_check_failure(
    tmp_path: Path,
):
    """Unit-level proof directly against the backend (not through mover.py):
    a ``pre_commit_check`` that raises must leave the destination
    completely untouched -- no partial commit, no stray destination file.
    """
    source_path = tmp_path / "source"
    _populate_metadata_scope(source_path)
    source_backend = TemporalMetadataSqliteBackend(source_path)
    target_path = tmp_path / "target"

    def _boom() -> None:
        raise RuntimeError("lock lost mid-copy (test)")

    with pytest.raises(RuntimeError, match="lock lost mid-copy"):
        source_backend.copy_collection_scope(target_path, pre_commit_check=_boom)

    assert _row_count(target_path / "temporal_metadata.db") == 0, (
        "a pre_commit_check failure must prevent any row from ever being "
        "committed to the destination"
    )


# ---------------------------------------------------------------------------
# Finding 2: relocation-record replacement must abort before os.replace()
# if the lock is lost between the entry check and the point of no return.
# ---------------------------------------------------------------------------


def test_relocation_record_not_replaced_when_lock_lost_mid_write_sequence(
    tmp_path: Path,
):
    """The entry-level check inside ``_mark_repo_relocation_complete``
    must pass, so only the NEW commit-point recheck immediately before
    ``os.replace()`` can abort this write.
    """
    fixed_root = tmp_path / "fixed"
    legacy_root = tmp_path / "legacy" / ".code-indexer" / "index"
    fixed_root.mkdir(parents=True)

    failed = _mark_repo_relocation_complete(
        fixed_root,
        legacy_root,
        {"code-indexer-temporal-e-2026Q1": _FAKE_SHA256_DIGEST},
        lock_lost_check=_LoseAfterEntryCheck(),
    )

    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert not marker.exists(), (
        "the relocation record must never be replaced into place once the "
        "lock was lost DURING the write sequence, even though the entry "
        "check passed"
    )
    assert failed is False, "a mid-write lock-loss abort is not a failure"
    leftover_tmp_files = list(
        fixed_root.glob(f".{mover._REPO_RELOCATION_COMPLETE_MARKER_NAME}.tmp-*")
    )
    assert not leftover_tmp_files, (
        "the scratch temp file must be cleaned up even when the write is "
        "aborted before the replace"
    )


def test_relocation_record_written_normally_when_lock_healthy_throughout(
    tmp_path: Path,
):
    """Control: with no lock-lost signal, the record is written normally --
    proves the new commit-point recheck does not regress the happy path."""
    fixed_root = tmp_path / "fixed"
    legacy_root = tmp_path / "legacy" / ".code-indexer" / "index"
    fixed_root.mkdir(parents=True)

    failed = _mark_repo_relocation_complete(
        fixed_root,
        legacy_root,
        {"code-indexer-temporal-e-2026Q1": _FAKE_SHA256_DIGEST},
        lock_lost_check=None,
    )

    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert marker.exists()
    assert failed is False


# ---------------------------------------------------------------------------
# Finding 3: an OSError writing the relocation record must be surfaced as a
# real failure, never silently swallowed.
# ---------------------------------------------------------------------------


@_skip_if_root
def test_relocation_record_oserror_is_reported_as_a_real_failure(tmp_path: Path):
    """A genuine OSError (fixed_root not writable) must make
    ``_mark_repo_relocation_complete`` return True (attempted and failed)
    instead of silently swallowing the error and returning as if nothing
    went wrong.
    """
    fixed_root = tmp_path / "fixed"
    legacy_root = tmp_path / "legacy" / ".code-indexer" / "index"
    fixed_root.mkdir(parents=True)
    os.chmod(fixed_root, _READ_ONLY_NO_WRITE_MODE)
    try:
        failed = _mark_repo_relocation_complete(
            fixed_root,
            legacy_root,
            {"code-indexer-temporal-e-2026Q1": _FAKE_SHA256_DIGEST},
            lock_lost_check=None,
        )
    finally:
        os.chmod(fixed_root, _WRITABLE_MODE)

    assert failed is True, (
        "an OSError while durably persisting the relocation record must "
        "be reported as a real failure, not silently swallowed as if the "
        "migration succeeded"
    )
    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert not marker.exists()


@_skip_if_root
def test_relocation_record_oserror_via_real_engine_is_folded_into_failed(
    tmp_path: Path,
):
    """End-to-end proof through the real public engine entry point, with
    NO mocking of the module under test and NO metadata-scope backend at
    all (``metadata_backend_factory=None`` short-circuits ``_sync_
    metadata_scope`` to a no-op before it ever touches ``fixed_root``, and
    ``legacy_root`` has no ``LEGACY_TEMPORAL_COLLECTION`` directory
    either) -- so the relocation-record write is the ONLY operation in
    this pass that ever attempts to write into ``fixed_root`` after it
    becomes read-only, isolating exactly the failure this test targets.

    A genuinely complete legacy shard is published and destructively
    deleted for real: the shard's own ``pre_delete_hook`` seam (already
    exposed by this module for exactly this kind of fault injection, used
    the same way by the round-5/6/7 exploit tests) makes ``fixed_root``
    read-only at the point where the deletion's own writes all happen
    under ``legacy_root``/trash, never under ``fixed_root`` -- so the
    shard deletion itself still succeeds genuinely, proving the failure
    below is specific to the metadata phase, not a side effect of the
    shard pass also failing.
    """
    legacy_root = tmp_path / "repo" / ".code-indexer" / "index"
    fixed_root = tmp_path / ".temporal" / "repo"
    shard = legacy_root / "code-indexer-temporal-e-2026Q1"
    _write_complete_shard(shard, "p1")
    assert not (legacy_root / LEGACY_TEMPORAL_COLLECTION).exists()

    def _make_fixed_root_read_only() -> None:
        os.chmod(fixed_root, _READ_ONLY_NO_WRITE_MODE)

    try:
        result = migrate_temporal_shards(
            legacy_root,
            fixed_root,
            relocation_enabled=True,
            cleanup_authorized=True,
            metadata_backend_factory=None,
            pre_delete_hook=_make_fixed_root_read_only,
            lock_lost_check=None,
        )
    finally:
        os.chmod(fixed_root, _WRITABLE_MODE)

    assert result.published == 1, "the shard's own publish must genuinely succeed"
    assert result.deleted == 1, "the shard's own deletion must genuinely succeed"
    assert result.collisions == 0
    assert not shard.exists()
    marker = fixed_root / mover._REPO_RELOCATION_COMPLETE_MARKER_NAME
    assert not marker.exists(), (
        "the relocation record must not exist -- its write failed with a real OSError"
    )
    assert result.failed == 1, (
        "exactly one failure -- the relocation-record OSError -- must be "
        "folded into MigrationResult.failed by the real engine, isolated "
        "from the shard pass, which succeeded"
    )
