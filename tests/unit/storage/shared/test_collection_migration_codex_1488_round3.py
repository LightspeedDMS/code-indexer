"""Story #1488 third-pass adversarial-review (Codex) remediation for the
shared per-collection consolidation engine (``collection_migration.py``).

Real files, REAL SQLite (the real ``ChunkStore``), no mocking of the code
under test -- failures are injected only via genuine filesystem conditions
(a path pre-created as a directory, a mutated/added legacy source file).
Each finding pairs its failing repro with a same-behavior regression guard
so the fix cannot over-correct the happy path (mirroring the convention in
``test_collection_migration_codex_1488.py``).

Two findings:

  * Codex Finding 4 (HIGH, still-open): the prior fix wrapped only the
    chunks.db BUILD in a typed exception envelope. The subsequent
    ``_write_content_manifest`` + ``_write_authoritative_vector_count`` +
    discriminator-prep steps ran OUTSIDE it, so a raw ``IsADirectoryError``
    (e.g. the content-manifest path pre-existing as a DIRECTORY) escaped
    AND left the freshly-built ``chunks.db`` on disk. The whole
    pre-discriminator lifecycle must be atomic-or-clean: either the
    discriminator flips over a fully-durable+manifested+counted chunks.db,
    or nothing is committed and the bad DB is gone (typed error, chained).

  * NEW finding (HIGH): on resume, a still-present legacy record that was
    missing/mismatched in chunks.db is correctly rebuilt into chunks.db,
    but the persisted content manifest and authoritative ``vector_count``
    are NEVER extended to include it. Cleanup then deletes ALL legacy
    files, so the rebuilt row becomes "unrecoverable" while absent from the
    stale manifest -- the exact-set check rejects the collection forever
    (``verify_collection_fully_migrated()`` False permanently; the next
    retry raises the terminal ``UnrecoverableConsolidationCorruptionError``).
    The resume path must atomically+durably rewrite the FULL manifest and
    authoritative count from the actual chunks.db contents AFTER the
    durability/integrity gate and BEFORE any legacy deletion.
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
    ConsolidationDurabilityError,
    _CONTENT_MANIFEST_FILENAME,
    _VECTOR_COUNT_META_KEY,
    _compute_record_content_digest,
    consolidate_collection_in_place,
    verify_collection_fully_migrated,
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


def _read_manifest_expected_count(collection_dir: Path) -> int:
    raw = json.loads((collection_dir / _CONTENT_MANIFEST_FILENAME).read_text())
    return int(raw["expected_count"])


def _read_authoritative_count(collection_dir: Path) -> int:
    raw = json.loads((collection_dir / "collection_meta.json").read_text())
    return int(raw[_VECTOR_COUNT_META_KEY])


def _manifest_recorded_digest(collection_dir: Path, point_id: str) -> str:
    raw = json.loads((collection_dir / _CONTENT_MANIFEST_FILENAME).read_text())
    return str(raw["records"][point_id])


# --------------------------------------------------------------------------
# Codex Finding 4: typed envelope MUST cover the manifest + count writes.
# --------------------------------------------------------------------------
class TestTypedEnvelopeCoversPreDiscriminatorWrites:
    def test_manifest_path_is_directory_converts_to_durability_error(
        self, tmp_path: Path
    ) -> None:
        """Codex real repro: the content-manifest path pre-exists as a
        DIRECTORY, so ``_write_content_manifest``'s ``os.replace`` raises a
        raw ``IsADirectoryError``. Post-fix that must convert to the typed
        ``ConsolidationDurabilityError`` (chained), the freshly-built
        chunks.db must be REMOVED, the legacy source untouched, and the
        collection must stay SHARDED_JSON (discriminator never committed)."""
        coll = tmp_path / "code-index-manifest-dir"
        _write_collection_meta(coll)
        legacy = _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])

        # Inject the fault: a DIRECTORY where the content manifest file must go.
        (coll / _CONTENT_MANIFEST_FILENAME).mkdir()

        with pytest.raises(ConsolidationDurabilityError):
            consolidate_collection_in_place(coll)

        # Atomic-or-clean: the uncommitted chunks.db is gone.
        assert not (coll / "chunks.db").exists(), (
            "freshly-built chunks.db was left on disk after a pre-discriminator "
            "manifest-write failure -- the pre-discriminator lifecycle is not "
            "atomic-or-clean"
        )
        # Legacy source untouched; discriminator never committed.
        assert legacy.exists()
        assert resolve_chunk_layout(coll) == ChunkLayout.SHARDED_JSON

    def test_normal_fresh_consolidation_still_writes_manifest_and_flips(
        self, tmp_path: Path
    ) -> None:
        """Regression guard (same-behavior): with no injected fault the fresh
        path still builds chunks.db, writes the manifest + authoritative
        count, and flips the discriminator (the extended envelope did not
        over-correct the happy path)."""
        coll = tmp_path / "code-index-happy"
        _write_collection_meta(coll)
        _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

        result = consolidate_collection_in_place(coll, deletion_authorized=True)

        assert result.status == "consolidated"
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
        assert _read_manifest_expected_count(coll) == 2
        assert _read_authoritative_count(coll) == 2
        assert verify_collection_fully_migrated(coll) is True


# --------------------------------------------------------------------------
# NEW finding: resume rebuild must reconcile the manifest + count.
# --------------------------------------------------------------------------
class TestResumeRebuildReconcilesManifest:
    def test_late_missing_record_rebuild_reconciles_manifest(
        self, tmp_path: Path
    ) -> None:
        """MANIFEST_STUCK repro (missing-record variant): a committed
        CHUNKS_DB collection gains a NEW still-present legacy file that is
        absent from chunks.db. Resume rebuilds it -- and post-fix must
        rewrite the manifest + authoritative count to match chunks.db
        BEFORE deleting legacy, so completion is reached (not poisoned)."""
        coll = tmp_path / "code-index-late-missing"
        _write_collection_meta(coll)
        _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        _write_vector_json(coll, "bb000000", [5.0, 6.0, 7.0, 8.0])

        # Build+verify+flip, legacy retained (bake window).
        consolidate_collection_in_place(coll, deletion_authorized=False)
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB
        assert _read_manifest_expected_count(coll) == 2

        # A LATE still-present legacy point appears that chunks.db never had.
        _write_vector_json(coll, "cc000000", [9.0, 9.0, 9.0, 9.0])

        # Resume: rebuild cc, reconcile manifest+count, then delete legacy.
        result = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert result.status == "already_consolidated"

        # chunks.db now has all three; manifest + count reflect that exactly.
        with ChunkStore(coll / "chunks.db") as store:
            assert store.count() == 3
        assert _read_manifest_expected_count(coll) == 3
        assert _read_authoritative_count(coll) == 3

        # No legacy files left; completion oracle returns TRUE (not stuck).
        assert next(coll.rglob("vector_*.json"), None) is None
        assert verify_collection_fully_migrated(coll) is True

        # A second run is a clean no-op -- never the terminal corruption error.
        result2 = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert result2.status == "already_consolidated"
        assert verify_collection_fully_migrated(coll) is True

    def test_late_mismatched_record_rebuild_reconciles_manifest(
        self, tmp_path: Path
    ) -> None:
        """MANIFEST_STUCK repro (mismatched-record variant): a still-present
        legacy record's content diverges from chunks.db (legacy source
        edited after the build). Resume rebuilds it to the new content; the
        manifest's stale digest for that key must be rewritten so the
        post-cleanup unrecoverable-digest check does not reject forever."""
        coll = tmp_path / "code-index-late-mismatch"
        _write_collection_meta(coll)
        _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        bb = _write_vector_json(
            coll, "bb000000", [5.0, 6.0, 7.0, 8.0], chunk_text="original"
        )

        consolidate_collection_in_place(coll, deletion_authorized=False)
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB

        # Mutate bb's still-present legacy source so it MISMATCHES chunks.db.
        bb.write_text(
            json.dumps(
                {
                    "id": "bb000000",
                    "vector": [5.0, 6.0, 7.0, 8.0],
                    "metadata": {"language": "python"},
                    "payload": {"path": "src/foo.py", "language": "python"},
                    "chunk_text": "EDITED-DIFFERENT-CONTENT",
                    "indexed_with_uncommitted_changes": True,
                }
            )
        )

        result = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert result.status == "already_consolidated"

        with ChunkStore(coll / "chunks.db") as store:
            assert store.count() == 2
        assert _read_manifest_expected_count(coll) == 2
        assert _read_authoritative_count(coll) == 2
        assert next(coll.rglob("vector_*.json"), None) is None
        assert verify_collection_fully_migrated(coll) is True

    def test_gated_resume_does_not_rewrite_manifest_but_authorized_does(
        self, tmp_path: Path
    ) -> None:
        """Codex New-High (Story #1488): the reconcile's O(N) manifest
        re-derive must fire ONLY when legacy is ACTUALLY about to be deleted
        (``deletion_authorized=True`` AND legacy present), NOT merely "legacy
        present".

        This replaces the prior vacuous ``..._leaves_manifest_intact`` test,
        which used a CLEAN manifest -- so a rewrite produced byte-identical
        content and its count-only assertions passed whether or not the
        reconcile ran. Here we seed a genuinely STALE-DIGEST manifest (manifest
        holds bb's ORIGINAL digest while chunks.db holds bb's EDITED content),
        so a rewrite is directly OBSERVABLE as a change in the recorded digest:

          * a ``deletion_authorized=False`` gated/bake-window resume leaves the
            manifest BYTE-IDENTICAL (no O(N) rewrite), preserves the stale
            digest, keeps the mixed layout, and deletes NOTHING; while
          * a ``deletion_authorized=True`` resume re-derives+rewrites the stale
            digest to match chunks.db BEFORE deleting legacy, and reaches
            completion (Codex Finding D preserved).
        """
        coll = tmp_path / "code-index-gated-vs-authorized"
        _write_collection_meta(coll)
        _write_vector_json(coll, "aa000000", [1.0, 2.0, 3.0, 4.0])
        bb = _write_vector_json(
            coll, "bb000000", [5.0, 6.0, 7.0, 8.0], chunk_text="original"
        )

        # Build+verify+flip, legacy retained (bake window): manifest holds bb's
        # ORIGINAL digest at this point.
        consolidate_collection_in_place(coll, deletion_authorized=False)
        assert resolve_chunk_layout(coll) == ChunkLayout.CHUNKS_DB

        # Seed the STALE-DIGEST state: edit bb's still-present legacy source AND
        # overwrite bb's chunks.db row with that edited content (real ChunkStore
        # as a fixture harness -- the SUT is untouched), leaving the persisted
        # manifest holding bb's now-stale ORIGINAL digest.
        edited = {
            "id": "bb000000",
            "vector": [5.0, 6.0, 7.0, 8.0],
            "metadata": {"language": "python"},
            "payload": {"path": "src/foo.py", "language": "python"},
            "chunk_text": "EDITED-DIFFERENT-CONTENT",
            "indexed_with_uncommitted_changes": True,
        }
        bb.write_text(json.dumps(edited))
        with ChunkStore(coll / "chunks.db", durable_synchronous=True) as store:
            store.write_batch([edited])
            store.flush_durable()
            actual_bb_digest = _compute_record_content_digest(store.read("bb000000"))

        stale_digest = _manifest_recorded_digest(coll, "bb000000")
        assert stale_digest != actual_bb_digest, (
            "test setup did not produce a stale manifest -- cannot observe "
            "rewrite-or-not behavior"
        )
        manifest_bytes_before = (coll / _CONTENT_MANIFEST_FILENAME).read_bytes()

        # GATED resume: must SKIP the rewrite entirely -- manifest byte-
        # identical, stale digest preserved, mixed layout kept, nothing deleted.
        gated = consolidate_collection_in_place(coll, deletion_authorized=False)
        assert gated.status == "already_consolidated"
        assert gated.deletion_gated is True
        assert (coll / _CONTENT_MANIFEST_FILENAME).read_bytes() == manifest_bytes_before
        assert _manifest_recorded_digest(coll, "bb000000") == stale_digest
        assert next(coll.rglob("vector_*.json"), None) is not None

        # AUTHORIZED resume: MUST re-derive+rewrite the stale digest to match
        # chunks.db BEFORE deleting legacy, then reach completion.
        authorized = consolidate_collection_in_place(coll, deletion_authorized=True)
        assert authorized.status == "already_consolidated"
        assert _manifest_recorded_digest(coll, "bb000000") == actual_bb_digest
        assert _read_manifest_expected_count(coll) == 2
        assert _read_authoritative_count(coll) == 2
        assert next(coll.rglob("vector_*.json"), None) is None
        assert verify_collection_fully_migrated(coll) is True
