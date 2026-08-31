"""Story #1488 adversarial-review (Codex) remediation for the shared
per-collection consolidation engine (``collection_migration.py``).

Real files, REAL SQLite (the real ``ChunkStore``), no mocking of the code
under test -- failures are injected only via genuine filesystem conditions.
Covers three findings (each with its failing repro AND a same-behavior
regression guard so the fix cannot over-correct the happy path):

  * Finding 1(b) -- ``_cleanup_old_sharded_files`` must delete ONLY the exact
    verified source paths captured in the original ``id_map`` snapshot, never
    a blind fresh ``rglob`` of ``vector_*.json``. A new/unexpected legacy
    file that appeared AFTER the verified scan (e.g. a concurrent foreground
    ``cidx index`` write) must NEVER be deleted -- cleanup aborts and
    surfaces it as an anomaly instead. Regression guard: a clean run with no
    unexpected files still deletes every verified legacy file.
  * Finding 3 -- the corrupt-chunks.db resume-repair path must atomically
    CLEAR the committed ``chunks_db`` discriminator BEFORE the destructive
    unlink+rebuild, so a rebuild failure leaves the collection resolving as
    SHARDED_JSON (readers see the intact legacy source), retryable -- never a
    committed CHUNKS_DB pointing at a partial/missing DB. Regression guard: a
    recoverable rebuild re-commits the discriminator to CHUNKS_DB on success.
  * Finding 4(a) -- the ENTIRE pre-discriminator chunks.db build lifecycle is
    wrapped in a typed durability/verification envelope: an invalid legacy
    record (``NonFiniteVectorError``) converts to a
    ``ConsolidationVerificationError`` (never a raw leak), and a low-level
    open/PRAGMA/fsync failure (e.g. ``chunks.db`` path is a directory)
    converts to a ``ConsolidationDurabilityError`` chained from the original;
    in both cases the partial chunks.db is removed and the legacy source is
    left untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationCleanupError,
    ConsolidationDurabilityError,
    ConsolidationVerificationError,
    consolidate_collection_in_place,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


# --------------------------------------------------------------------------
# Helpers (mirror the real sharded record shape).
# --------------------------------------------------------------------------
def _write_vector_json(
    collection_dir: Path,
    point_id: str,
    vector,
    *,
    path: str = "src/foo.py",
    chunk_text: str = "chunk",
) -> Path:
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": path, "language": "python"},
        "chunk_text": chunk_text,
        "indexed_with_uncommitted_changes": True,
    }
    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path, vector_size: int = 4) -> None:
    collection_dir.mkdir(parents=True, exist_ok=True)
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": collection_dir.name, "vector_size": vector_size})
    )


# --------------------------------------------------------------------------
# Finding 1(b): cleanup deletes ONLY verified id_map paths.
# --------------------------------------------------------------------------
class TestCleanupDeletesOnlyVerifiedPaths:
    def test_unexpected_new_legacy_file_is_never_deleted(self, tmp_path: Path) -> None:
        """Codex AC7 repro (primitive-level): cleanup is handed the exact
        verified snapshot {p}. A NEW legacy point q appears on disk AFTER the
        snapshot (a concurrent foreground `cidx index` write). Cleanup must
        NEVER delete the unverified q, and surface it as a cleanup anomaly."""
        from code_indexer.storage.shared.collection_migration import (
            _cleanup_old_sharded_files,
        )

        coll = tmp_path / "code-index-abc"
        _write_collection_meta(coll)
        p_file = _write_vector_json(coll, "pp000000", [1.0, 2.0, 3.0, 4.0])
        # The verified snapshot captured at consolidation start: ONLY p.
        verified_paths = {p_file}

        # A NEW legacy point q appears AFTER the snapshot -- NOT verified.
        q_file = _write_vector_json(coll, "qq111111", [9.0, 9.0, 9.0, 9.0])

        with pytest.raises(ConsolidationCleanupError):
            _cleanup_old_sharded_files(coll, verified_paths)

        # q (unverified) is NEVER deleted.
        assert q_file.exists(), "unverified concurrently-written point q was deleted"
        # Abort-before-delete: on detecting an unexpected file, cleanup
        # touches NOTHING and surfaces the anomaly, so even the verified p is
        # left on disk (a harmless orphan retried once the anomaly is
        # resolved) -- never a partial, half-deleted state.
        assert p_file.exists()

    def test_normal_cleanup_deletes_exactly_the_verified_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: with no unexpected files, cleanup deletes every
        verified file in the snapshot and prunes empty shard subdirs."""
        from code_indexer.storage.shared.collection_migration import (
            _cleanup_old_sharded_files,
        )

        coll = tmp_path / "code-index-clean"
        _write_collection_meta(coll)
        f0 = _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        f1 = _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

        deleted = _cleanup_old_sharded_files(coll, {f0, f1})

        assert deleted == 2
        assert not f0.exists()
        assert not f1.exists()
        assert next(coll.rglob("vector_*.json"), None) is None


# --------------------------------------------------------------------------
# Finding 3: resume rebuild failure -> discriminator reverts to SHARDED_JSON.
# --------------------------------------------------------------------------
class TestResumeRebuildClearsDiscriminatorOnFailure:
    def test_failed_resume_repair_leaves_sharded_json_retryable(
        self, tmp_path: Path
    ) -> None:
        coll = tmp_path / "code-index-resume"
        _write_collection_meta(coll)
        f0 = _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        f1 = _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

        # Build+verify+flip, legacy retained (bake window): committed
        # CHUNKS_DB + manifest + vector_count + legacy present.
        consolidate_collection_in_place(coll, deletion_authorized=False)
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
        assert f0.exists() and f1.exists()

        # REAL failure injection: replace chunks.db with a DIRECTORY. The
        # resume gate sees a non-database (fails), classifies the repair as
        # recoverable (legacy fully present), CLEARS the discriminator, then
        # its destructive unlink raises IsADirectoryError -- a genuine
        # rebuild failure with no mocking of the engine.
        chunks_db = coll / "chunks.db"
        chunks_db.unlink()
        chunks_db.mkdir()

        with pytest.raises(Exception):
            consolidate_collection_in_place(coll, deletion_authorized=True)

        # The committed discriminator must have been CLEARED before the
        # destructive rebuild -- readers now correctly resolve SHARDED_JSON
        # and see the intact legacy source.
        assert resolve_chunk_layout(coll) == ChunkLayout.SHARDED_JSON
        assert f0.exists() and f1.exists()

        # Retryable: remove the bad artifact and a fresh consolidation
        # succeeds against the still-intact legacy source.
        chunks_db.rmdir()
        consolidate_collection_in_place(coll, deletion_authorized=True)
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
        with ChunkStore(coll / "chunks.db") as store:
            assert store.count() == 2

    def test_recoverable_resume_repair_recommits_discriminator(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: when the rebuild SUCCEEDS, the discriminator is
        re-committed to CHUNKS_DB (cleared only transiently across the
        destructive step)."""
        coll = tmp_path / "code-index-recover"
        _write_collection_meta(coll)
        _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

        consolidate_collection_in_place(coll, deletion_authorized=False)
        (coll / "chunks.db").write_bytes(b"corrupt-but-legacy-intact")

        # Resume with legacy intact -> recoverable rebuild -> cleanup runs.
        result = consolidate_collection_in_place(coll, deletion_authorized=True)

        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
        assert result.status == "already_consolidated"
        assert next(coll.rglob("vector_*.json"), None) is None
        with ChunkStore(coll / "chunks.db") as store:
            assert store.count() == 2


# --------------------------------------------------------------------------
# Finding 4(a): typed durability/verification envelope over the whole build.
# --------------------------------------------------------------------------
class TestTypedDurabilityEnvelope:
    def test_non_finite_vector_converts_to_verification_error(
        self, tmp_path: Path
    ) -> None:
        coll = tmp_path / "code-index-nan"
        _write_collection_meta(coll)
        good = _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        # NaN vector -> ChunkStore.write_batch raises NonFiniteVectorError.
        bad = _write_vector_json(coll, "bb000000", [float("nan"), 1.0, 2.0, 3.0])

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(coll)

        # Partial chunks.db removed; legacy untouched; not flipped.
        assert not (coll / "chunks.db").exists()
        assert good.exists() and bad.exists()
        assert resolve_chunk_layout(coll) == ChunkLayout.SHARDED_JSON

    def test_chunks_db_path_is_directory_converts_to_durability_error(
        self, tmp_path: Path
    ) -> None:
        coll = tmp_path / "code-index-dir"
        _write_collection_meta(coll)
        legacy = _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        # A directory sitting where chunks.db must be -> low-level open/unlink
        # failure that must convert to the typed durability error, never leak
        # a raw IsADirectoryError.
        (coll / "chunks.db").mkdir()

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(coll)

        # Legacy untouched; not flipped.
        assert legacy.exists()
        assert resolve_chunk_layout(coll) == ChunkLayout.SHARDED_JSON
