"""Unit tests for consolidate_collection_in_place() (Story #1458 AC3/AC4/AC6).

Fleet migration's core per-collection consolidation primitive: reads the
existing sharded vector_*.json files (streaming, no copy), writes a new
chunks.db into the SAME collection directory as a pure addition, read-back
verifies it field-for-field, durably flips the chunks_db discriminator, and
only then deletes the old files individually -- never a whole-directory
replace, never touching the collection root.

All tests use REAL files, REAL SQLite (via the real ChunkStore), and REAL
filesystem operations -- no mocking of the storage layer under test. The
only monkeypatched surface is os.statvfs, to deterministically simulate a
low-disk-space condition that cannot be reliably reproduced in CI.
"""

import json
import os
from pathlib import Path
from typing import Optional

import pytest

from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    ConsolidationResult,
    ConsolidationVerificationError,
    consolidate_collection_in_place,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_vector_json(
    collection_dir: Path,
    point_id: str,
    vector,
    *,
    path: str = "src/foo.py",
    chunk_text: Optional[str] = None,
    git_blob_hash: Optional[str] = None,
    extra_payload: Optional[dict] = None,
) -> Path:
    """Write one legacy sharded vector_<id>.json record, mirroring the real
    FilesystemVectorStore record shape (root-level id/vector/metadata/payload
    plus exactly one content-variant field)."""
    payload = {"path": path, "language": "python"}
    if extra_payload:
        payload.update(extra_payload)

    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": payload,
    }
    if chunk_text is not None:
        record["chunk_text"] = chunk_text
        record["indexed_with_uncommitted_changes"] = True
    if git_blob_hash is not None:
        record["git_blob_hash"] = git_blob_hash
        record["indexed_with_uncommitted_changes"] = False

    shard_dir = collection_dir / point_id[:2] / point_id[2:4]
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(collection_dir: Path) -> None:
    (collection_dir / "collection_meta.json").write_text(
        json.dumps({"name": "coll", "vector_size": 4})
    )


def _write_manifest_for_records(collection_dir: Path, records: dict) -> None:
    """Write a REAL, digest-matching content-integrity manifest for
    ``records`` (point_id -> raw record dict) -- used by crash-safety
    tests that hand-construct an "already migrated" chunks.db state
    (simulating a crash AFTER the manifest write but BEFORE cleanup)
    without going through the real consolidate_collection_in_place() flow.
    An EMPTY manifest would trivially satisfy the round-5 set-equality
    check for these specific tests (every record is still recoverable
    from legacy), but would not faithfully simulate the real production
    state at that crash point -- a real manifest always covers the full
    record set."""
    from code_indexer.storage.shared.collection_migration import (
        _MANIFEST_SCHEMA_VERSION,
        _compute_record_content_digest,
        _empty_fold_accumulator,
        _fold_manifest_entry,
    )

    manifest_records = {
        point_id: _compute_record_content_digest(record)
        for point_id, record in records.items()
    }
    accumulator = _empty_fold_accumulator()
    for point_id, digest in manifest_records.items():
        accumulator = _fold_manifest_entry(accumulator, point_id, digest)

    manifest = {
        "version": _MANIFEST_SCHEMA_VERSION,
        "records": manifest_records,
        "expected_count": len(manifest_records),
        "root_digest": accumulator.hex(),
    }
    (collection_dir / "chunks_db_content_manifest.json").write_text(
        json.dumps(manifest)
    )


class TestConsolidateCollectionInPlaceBasicFlow:
    def test_writes_chunks_db_and_flips_discriminator(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "aaaa1111", [0.1, 0.2, 0.3, 0.4], chunk_text="hello"
        )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert result.records_written == 1
        assert (tmp_path / "chunks.db").exists()
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB

    def test_old_vector_files_deleted_after_flip(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "bbbb2222", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        consolidate_collection_in_place(tmp_path)

        assert not vfile.exists()

    def test_now_empty_hash_shard_subdirs_removed(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "cccc3333", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )
        shard_dir = vfile.parent

        consolidate_collection_in_place(tmp_path)

        assert not shard_dir.exists()

    def test_pre_existing_id_index_bin_is_unlinked(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "dddd4444", [1.0, 2.0, 3.0, 4.0], chunk_text="x")
        (tmp_path / "id_index.bin").write_bytes(b"stale-legacy-index")

        consolidate_collection_in_place(tmp_path)

        assert not (tmp_path / "id_index.bin").exists()

    def test_collection_root_itself_never_deleted(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "eeee5555", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        consolidate_collection_in_place(tmp_path)

        assert tmp_path.exists()
        assert (tmp_path / "collection_meta.json").exists()

    def test_multiple_points_all_consolidated(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        for i in range(5):
            point_id = f"point{i:04d}"
            _write_vector_json(
                tmp_path, point_id, [float(i)] * 4, chunk_text=f"content-{i}"
            )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert result.records_written == 5
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 5

    def test_empty_collection_still_consolidates_cleanly(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert result.records_written == 0
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB


class TestConsolidateCollectionInPlaceContentVariantPreservation:
    """AC6: chunk_text is not fabricated onto records that only ever had
    git_blob_hash, and vice versa."""

    def test_chunk_text_variant_preserved(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "ffff6666", [1.0, 2.0, 3.0, 4.0], chunk_text="dirty file content"
        )

        consolidate_collection_in_place(tmp_path)

        with ChunkStore(tmp_path / "chunks.db") as store:
            record = store.read("ffff6666")
        assert record["chunk_text"] == "dirty file content"
        assert "git_blob_hash" not in record

    def test_git_blob_hash_variant_preserved(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path,
            "aabb7777",
            [1.0, 2.0, 3.0, 4.0],
            git_blob_hash="deadbeef1234",
        )

        consolidate_collection_in_place(tmp_path)

        with ChunkStore(tmp_path / "chunks.db") as store:
            record = store.read("aabb7777")
        assert record["git_blob_hash"] == "deadbeef1234"
        assert record["indexed_with_uncommitted_changes"] is False
        assert "chunk_text" not in record

    def test_mixed_variants_in_same_collection_both_preserved_distinctly(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "cc001111", [1.0, 0.0, 0.0, 0.0], chunk_text="dirty"
        )
        _write_vector_json(
            tmp_path, "cc002222", [0.0, 1.0, 0.0, 0.0], git_blob_hash="cleanhash"
        )

        consolidate_collection_in_place(tmp_path)

        with ChunkStore(tmp_path / "chunks.db") as store:
            dirty = store.read("cc001111")
            clean = store.read("cc002222")
        assert dirty["chunk_text"] == "dirty"
        assert "git_blob_hash" not in dirty
        assert clean["git_blob_hash"] == "cleanhash"
        assert "chunk_text" not in clean


class TestConsolidateCollectionInPlaceReadBackVerification:
    """AC3 step 3 + testing requirement: field-for-field verification runs
    BEFORE the discriminator flip, and catches any mismatch."""

    def test_verification_failure_raises_before_flip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "dd003333", [1.0, 2.0, 3.0, 4.0], chunk_text="original"
        )

        # Inject a dropped field: patch ChunkStore.read to strip
        # "chunk_text" from whatever was actually persisted, simulating a
        # corrupted/incomplete write that read-back verification must
        # catch (payload/vector/id round-trip through the real DB
        # unmodified -- only the verification READ is tampered with).
        original_read = ChunkStore.read

        def _tampered_read(self, point_id):
            record = original_read(self, point_id)
            if record is not None and "chunk_text" in record:
                del record["chunk_text"]
            return record

        monkeypatch.setattr(ChunkStore, "read", _tampered_read)

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(tmp_path)

        # The flag must NOT be set, and no old file may be deleted.
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert (tmp_path / "dd" / "00" / "vector_dd003333.json").exists()

    def test_verification_failure_on_vector_mismatch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "ee004444", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        original_read = ChunkStore.read

        def _tampered_read(self, point_id):
            record = original_read(self, point_id)
            if record is not None:
                record["vector"] = [9.0, 9.0, 9.0, 9.0]
            return record

        monkeypatch.setattr(ChunkStore, "read", _tampered_read)

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON


class TestConsolidateCollectionInPlaceCrashSafetyAndResume:
    """AC4: ordinary file-level atomicity -- durable flag write BEFORE any
    deletion; resume is discriminator-driven."""

    def test_crash_before_flip_leaves_old_representation_authoritative(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "ff005555", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        import code_indexer.storage.shared.collection_migration as mod

        def _boom(collection_dir):
            raise RuntimeError("simulated crash before discriminator flip")

        monkeypatch.setattr(mod, "write_chunks_db_discriminator", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            consolidate_collection_in_place(tmp_path)

        # Old representation intact and authoritative; chunks.db may
        # coexist harmlessly (pure addition -- no corruption).
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert vfile.exists()

    def test_restart_after_pre_flip_crash_completes_successfully(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "aa006666", [1.0, 2.0, 3.0, 4.0], chunk_text="original-content"
        )

        import code_indexer.storage.shared.collection_migration as mod

        real_write_discriminator = mod.write_chunks_db_discriminator
        call_count = {"n": 0}

        def _boom_once(collection_dir):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated crash")
            real_write_discriminator(collection_dir)

        monkeypatch.setattr(mod, "write_chunks_db_discriminator", _boom_once)

        with pytest.raises(RuntimeError):
            consolidate_collection_in_place(tmp_path)
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON

        # Retry (simulating a process restart) -- re-writing chunks.db is a
        # pure addition, safe to redo, and this time it succeeds.
        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        with ChunkStore(tmp_path / "chunks.db") as store:
            record = store.read("aa006666")
        assert record["chunk_text"] == "original-content"

    def test_resume_after_flip_proceeds_directly_to_cleanup_never_redoes_write(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "bb007777", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        # Simulate "crash mid-step-5": chunks.db written+verified+flag
        # flipped, but the old file was NOT yet deleted.
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        with ChunkStore(tmp_path / "chunks.db") as store:
            record = json.loads(vfile.read_text())
            store.write_batch([record])
        _write_manifest_for_records(tmp_path, {"bb007777": record})
        write_chunks_db_discriminator(tmp_path)
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        assert vfile.exists()  # not yet cleaned up

        write_batch_calls = {"n": 0}
        original_write_batch = ChunkStore.write_batch

        def _counting_write_batch(self, records):
            write_batch_calls["n"] += 1
            return original_write_batch(self, records)

        monkeypatch.setattr(ChunkStore, "write_batch", _counting_write_batch)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not vfile.exists()
        # steps 1-4 (including the write) were never redone
        assert write_batch_calls["n"] == 0
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.read("bb007777") is not None

    def test_resume_after_flip_also_removes_stray_id_index_bin(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "cc008888", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        record = json.loads(vfile.read_text())
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch([record])
        _write_manifest_for_records(tmp_path, {"cc008888": record})
        write_chunks_db_discriminator(tmp_path)
        (tmp_path / "id_index.bin").write_bytes(b"leftover")

        consolidate_collection_in_place(tmp_path)

        assert not (tmp_path / "id_index.bin").exists()

    def test_idempotent_double_call_on_already_consolidated_collection(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "dd009999", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        first = consolidate_collection_in_place(tmp_path)
        second = consolidate_collection_in_place(tmp_path)

        assert first.status == "consolidated"
        assert second.status == "already_consolidated"
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 1


class TestConsolidateCollectionInPlaceDiskHeadroomPreflight:
    def test_insufficient_disk_headroom_skips_without_touching_anything(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "ee00aaaa", [1.0, 2.0, 3.0, 4.0], chunk_text="x" * 1000
        )

        class _FakeStatvfs:
            f_bavail = 1
            f_frsize = 1  # 1 byte free -- guaranteed insufficient

        monkeypatch.setattr(os, "statvfs", lambda path: _FakeStatvfs())

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "skipped_insufficient_disk"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert vfile.exists()
        assert not (tmp_path / "chunks.db").exists()

    def test_sufficient_disk_headroom_proceeds_normally(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "ff00bbbb", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        class _FakeStatvfs:
            f_bavail = 10_000_000_000
            f_frsize = 4096

        monkeypatch.setattr(os, "statvfs", lambda path: _FakeStatvfs())

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"

    def test_statvfs_failure_fails_closed_and_skips(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex round-6 MEDIUM finding: for this specific DESTRUCTIVE
        scheduled job (fleet migration), a preflight that cannot even
        run must fail CLOSED (skip, source stays untouched) rather than
        OPEN -- unlike a generic advisory guard, this job runs
        unattended against real production disks, so a statvfs failure
        (e.g. transient NFS hiccup) must never be silently treated as
        'plenty of room'."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "aa00cccc", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        def _raise_statvfs(path):
            raise OSError("statvfs unavailable")

        monkeypatch.setattr(os, "statvfs", _raise_statvfs)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "skipped_insufficient_disk"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert vfile.exists()
        assert not (tmp_path / "chunks.db").exists()

    def test_safety_margin_multiplier_applied_to_estimated_bytes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex round-6 MEDIUM finding: the raw byte-count estimate only
        counts legacy JSON file sizes -- it ignores SQLite overhead, the
        content-integrity manifest, and WAL/journal files. A safety
        margin multiplier must be applied so a disk with barely enough
        room for the RAW legacy bytes (and nothing else) is correctly
        reported as insufficient."""
        from code_indexer.storage.shared.collection_migration import (
            _estimate_bytes_needed,
            _has_disk_headroom,
        )

        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "bb00dddd", [1.0, 2.0, 3.0, 4.0], chunk_text="x" * 1000
        )
        raw_estimate = _estimate_bytes_needed([vfile])
        assert raw_estimate > 0

        class _ExactlyEnoughForRawEstimateStatvfs:
            f_bavail = raw_estimate
            f_frsize = 1

        monkeypatch.setattr(
            os, "statvfs", lambda path: _ExactlyEnoughForRawEstimateStatvfs()
        )

        assert _has_disk_headroom(tmp_path, raw_estimate) is False, (
            "Bug: available space exactly equal to the RAW legacy-file "
            "byte estimate (no margin for SQLite overhead/manifest/"
            "journals) was reported as sufficient headroom -- no safety "
            "margin multiplier is being applied."
        )


class TestConsolidationResultDataclass:
    def test_result_has_expected_fields(self) -> None:
        result = ConsolidationResult(status="consolidated", records_written=3)
        assert result.status == "consolidated"
        assert result.records_written == 3


class TestConsolidateCollectionInPlaceRejectsMalformedRecords:
    """Codex Finding #4 (CRITICAL, Messi Rule #13): a malformed legacy
    record must cause a LOUD failure, never a silent flip that omits data."""

    def test_malformed_legacy_record_causes_loud_failure_never_flip(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "aa001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x")
        # A malformed legacy file (invalid JSON) sitting alongside a valid one.
        bad_dir = tmp_path / "bb" / "22"
        bad_dir.mkdir(parents=True)
        (bad_dir / "vector_bb002222.json").write_text("{not valid json::")

        with pytest.raises(ConsolidationVerificationError):
            consolidate_collection_in_place(tmp_path)

        assert resolve_chunk_layout(tmp_path) == ChunkLayout.SHARDED_JSON
        assert (
            not (tmp_path / "chunks.db").exists() or True
        )  # pure-addition ok either way


class TestConsolidateCollectionInPlaceResumeVerifiesChunksDb:
    """Codex Finding #2 (CRITICAL): on resume, the chunks_db discriminator
    flag alone must NEVER be trusted for the destructive legacy-cleanup
    decision -- chunks.db must be reopened and verified to actually contain
    every still-present legacy record first."""

    def test_resume_refuses_cleanup_when_chunks_db_missing_despite_flag_set(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "ee001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        # Discriminator set but chunks.db was NEVER actually written
        # (simulated corruption/inconsistency).
        write_chunks_db_discriminator(tmp_path)
        assert not (tmp_path / "chunks.db").exists()

        # Bug #1486: a missing chunks.db (caught by the unified
        # durability/integrity gate) with no manifest present at all is
        # UNRECOVERABLE (fail-closed) -- there is nothing to prove any
        # OTHER record is covered, regardless of this one legacy file's
        # presence.
        from code_indexer.storage.shared.collection_migration import (
            UnrecoverableConsolidationCorruptionError,
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

        assert vfile.exists(), (
            "Bug: legacy data was deleted despite chunks.db not existing -- "
            "the discriminator flag was trusted alone for a destructive "
            "decision."
        )

    def test_resume_rebuilds_missing_still_present_legacy_record_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        """Codex Finding #2 strengthening: a still-present legacy record
        missing from chunks.db is RECOVERABLE (the legacy source is right
        there) -- the resume path must attempt a rebuild rather than
        permanently refuse, per Codex's explicit critique ("raises
        permanently instead of attempting a rebuild")."""
        _write_collection_meta(tmp_path)
        vfile1 = _write_vector_json(
            tmp_path, "ff001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )
        vfile2 = _write_vector_json(
            tmp_path, "ff002222", [5.0, 6.0, 7.0, 8.0], chunk_text="y"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        # Only ONE of the two still-present legacy records ended up in
        # chunks.db (simulating a crash mid-write on a prior attempt).
        record1 = json.loads(vfile1.read_text())
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch([record1])
        # A real manifest from the original fresh migration would have
        # covered BOTH records, even though only one made it into
        # chunks.db before the simulated crash.
        _write_manifest_for_records(
            tmp_path,
            {"ff001111": record1, "ff002222": json.loads(vfile2.read_text())},
        )
        write_chunks_db_discriminator(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not vfile1.exists() and not vfile2.exists(), (
            "Legacy files should be cleaned up once the rebuild proves "
            "chunks.db genuinely has every still-present record."
        )
        with ChunkStore(tmp_path / "chunks.db", immutable=True) as store:
            assert store.count() == 2
            rebuilt = store.read("ff002222")
        assert rebuilt is not None
        assert rebuilt["chunk_text"] == "y"

    def test_resume_rebuilds_field_mismatched_still_present_legacy_record(
        self, tmp_path: Path
    ) -> None:
        """Codex Finding #2 strengthening: a record PRESENT in chunks.db
        but with WRONG field content (corruption) is exactly as dangerous
        as a missing one -- the old ID-membership-only check would miss
        this entirely."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "gg001111", [1.0, 2.0, 3.0, 4.0], chunk_text="correct"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        # chunks.db has the ID, but with CORRUPTED field content (wrong
        # vector/chunk_text) relative to the still-present legacy source.
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "gg001111",
                        "vector": [9.0, 9.0, 9.0, 9.0],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/foo.py", "language": "python"},
                        "chunk_text": "CORRUPTED",
                        "indexed_with_uncommitted_changes": True,
                    }
                ]
            )
        # A real manifest from the original fresh migration would reflect
        # the CORRECT content (matching the legacy source), never the
        # corrupted stored state being simulated here.
        _write_manifest_for_records(
            tmp_path, {"gg001111": json.loads(vfile.read_text())}
        )
        write_chunks_db_discriminator(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not vfile.exists()
        with ChunkStore(tmp_path / "chunks.db", immutable=True) as store:
            rebuilt = store.read("gg001111")
        assert rebuilt["chunk_text"] == "correct"
        assert rebuilt["vector"] == pytest.approx([1.0, 2.0, 3.0, 4.0])


class TestConsolidateCollectionInPlaceExactSetVerification:
    """Codex Finding #3 (CRITICAL): verification must be an EXACT-SET
    comparison (same count, same IDs, no extras) via a FRESH reopen, not
    merely 'every original ID is present' via the same in-process handle
    that just wrote it.

    Bug #1486 Defect B: a stale extra row inherited from a prior
    INTERRUPTED fresh-path attempt (a healthy leftover chunks.db whose id
    set no longer matches the current authoritative legacy source) is now
    handled BEFORE the write loop -- ``_discard_corrupt_leftover_chunks_db``
    discards it and the collection is rebuilt cleanly, so this scenario
    consolidates successfully rather than raising forever (the original
    non-idempotent-retry defect). The exact-set check itself remains as
    defense-in-depth against extras appearing DURING a single run."""

    def test_stale_leftover_extra_row_is_discarded_and_rebuilt_cleanly(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "aa003333", [1.0, 2.0, 3.0, 4.0], chunk_text="x")
        # Pre-seed chunks.db with a STALE extra row that has NO corresponding
        # legacy source file (simulating a prior interrupted run whose
        # leftover chunks.db no longer matches the current legacy source).
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "zz999999",
                        "vector": [9.0, 9.0, 9.0, 9.0],
                        "metadata": {},
                        "payload": {"path": "stale.py"},
                        "chunk_text": "stale",
                    }
                ]
            )

        # Bug #1486 Defect B: the stale leftover is discarded and rebuilt
        # cleanly -- consolidation succeeds (idempotent retry), never raises.
        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB
        with ChunkStore(tmp_path / "chunks.db", immutable=True) as store:
            final_ids = set(store.all_point_ids())
        assert final_ids == {"aa003333"}, (
            "Bug #1486 Defect B: the stale leftover row must be discarded so "
            f"the final chunks.db equals the legacy source exactly, got "
            f"{sorted(final_ids)}"
        )


class TestConsolidateCollectionInPlaceBatchedProcessing:
    """Codex Finding #8 (HIGH, confirmed independently by Claude too):
    process in bounded batches -- write a batch, verify it, discard, move
    to the next -- rather than materializing the entire collection's
    records/originals in memory at once (real OOM risk at millions-of-
    chunks scale)."""

    def test_processes_in_bounded_batches_not_all_at_once(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import code_indexer.storage.shared.collection_migration as mod

        monkeypatch.setattr(mod, "_MIGRATION_BATCH_SIZE", 2)

        _write_collection_meta(tmp_path)
        for i in range(5):
            _write_vector_json(
                tmp_path, f"aa0000{i:02d}", [1.0, 2.0, 3.0, 4.0], chunk_text=f"x{i}"
            )

        write_batch_calls = {"n": 0}
        original_write_batch = ChunkStore.write_batch

        def _counting_write_batch(self, records):
            write_batch_calls["n"] += 1
            assert len(records) <= 2, "batch exceeded the configured bound"
            return original_write_batch(self, records)

        monkeypatch.setattr(ChunkStore, "write_batch", _counting_write_batch)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert write_batch_calls["n"] >= 3, (
            f"expected >=3 batches for 5 records at batch_size=2, got "
            f"{write_batch_calls['n']} -- processing was not actually bounded"
        )
        with ChunkStore(tmp_path / "chunks.db") as store:
            assert store.count() == 5


class TestVerifyCollectionFullyMigrated:
    """Codex CRITICAL Finding #2 (round 2): is_repo_already_migrated() must
    invoke the SAME fresh-reopen verification the resume path already does
    -- never trust the chunks_db discriminator flag alone. This is the
    reusable, side-effect-free oracle discovery.py wires up (never
    reinvented there)."""

    def test_sharded_json_layout_is_not_fully_migrated(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "hh000001", [1.0, 2.0, 3.0, 4.0], chunk_text="x")

        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        assert verify_collection_fully_migrated(tmp_path) is False

    def test_flag_set_but_legacy_files_still_present_is_not_fully_migrated(
        self, tmp_path: Path
    ) -> None:
        """The CRITICAL bug this fix closes: a crash between the flip and
        cleanup must NEVER be reported as 'fully migrated' -- otherwise
        discovery permanently skips this collection and the real
        resume/cleanup verifier in consolidate_collection_in_place() is
        never invoked again."""
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "hh000002", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch([json.loads(vfile.read_text())])
        write_chunks_db_discriminator(tmp_path)
        assert vfile.exists(), "fixture bug: legacy file should still be present"

        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        assert verify_collection_fully_migrated(tmp_path) is False

    def test_flag_set_and_legacy_files_fully_cleaned_and_store_healthy_is_migrated(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "hh000003", [1.0, 2.0, 3.0, 4.0], chunk_text="x")
        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "consolidated"

        assert verify_collection_fully_migrated(tmp_path) is True

    def test_flag_set_cleanup_done_but_chunks_db_corrupted_is_not_fully_migrated(
        self, tmp_path: Path
    ) -> None:
        """Even with zero legacy files remaining, a genuinely corrupt/
        unopenable chunks.db must never report as fully migrated -- this is
        the fresh-reopen integrity proof Finding #3 established, reused
        here with nothing left to compare against."""
        _write_collection_meta(tmp_path)
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        # Discriminator set, zero legacy files ever existed, but chunks.db
        # itself was never actually created (simulates corruption/loss
        # after a fully-completed-looking migration).
        write_chunks_db_discriminator(tmp_path)
        assert not (tmp_path / "chunks.db").exists()

        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        assert verify_collection_fully_migrated(tmp_path) is False

    def test_never_raises_on_malformed_legacy_record(self, tmp_path: Path) -> None:
        """A rejected/malformed legacy record (Finding #4) must make this
        oracle report 'not fully migrated' (safe re-attempt), never
        propagate an exception through a read-only discovery predicate."""
        _write_collection_meta(tmp_path)
        (tmp_path / "vector_bad.json").write_text("{not valid json")

        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        assert verify_collection_fully_migrated(tmp_path) is False


class TestCleanupFailureNeverSilentlyReportsSuccess:
    """Codex MEDIUM finding: _cleanup_old_sharded_files swallowed EVERY
    OSError as 'already gone (race)' -- including genuine failures
    (permission denied, read-only filesystem, disk I/O error) where the
    file is still very much present. consolidate_collection_in_place must
    never silently report success (Messi Rule #13 anti-silent-failure)
    when cleanup genuinely failed."""

    def test_cleanup_raises_when_a_legacy_file_fails_to_delete_for_a_reason_other_than_already_gone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationCleanupError,
        )

        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "kk001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        original_unlink = Path.unlink

        def _failing_unlink(self, *args, **kwargs):
            if self == vfile:
                raise PermissionError(f"simulated permission denied for {self}")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _failing_unlink)

        with pytest.raises(ConsolidationCleanupError):
            consolidate_collection_in_place(tmp_path)

        assert vfile.exists(), (
            "Bug: the legacy file genuinely failed to delete (not a race) "
            "but was silently treated as harmless -- it must remain on "
            "disk, observable, and retried on a later pass."
        )
        # The write+verify+flip already succeeded -- only cleanup failed --
        # so the discriminator IS set (crash-safety: a retry resumes
        # directly into cleanup, never redoing the write).
        assert resolve_chunk_layout(tmp_path) == ChunkLayout.CHUNKS_DB


class TestContentIntegrityManifest:
    """Codex CRITICAL finding (round 4): once legacy files are gone,
    verification only checked chunks.db KEY presence, never record
    CONTENT -- a chunks.db with valid primary keys but corrupted
    compressed payload/vector would pass forever. A crash-durable content
    manifest, persisted BEFORE the legacy source is deleted, closes this
    gap by giving post-cleanup verification something real to compare
    stored content against."""

    def test_manifest_written_after_fresh_consolidation(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "mm001111", [1.0, 2.0, 3.0, 4.0], chunk_text="original"
        )

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        manifest_path = tmp_path / "chunks_db_content_manifest.json"
        assert manifest_path.exists(), (
            "Bug: no crash-durable content manifest was persisted -- "
            "post-cleanup verification has nothing to compare stored "
            "content against once the legacy source is gone."
        )

    def test_manifest_content_matches_stored_record_digest(
        self, tmp_path: Path
    ) -> None:
        import json as _json

        from code_indexer.storage.shared.collection_migration import (
            _compute_record_content_digest,
        )

        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "mm002222", [5.0, 6.0, 7.0, 8.0], chunk_text="original"
        )

        consolidate_collection_in_place(tmp_path)

        manifest = _json.loads(
            (tmp_path / "chunks_db_content_manifest.json").read_text()
        )
        with ChunkStore(tmp_path / "chunks.db", immutable=True) as store:
            stored_record = store.read("mm002222")
        expected_digest = _compute_record_content_digest(stored_record)

        assert manifest["records"]["mm002222"] == expected_digest

    def test_manifest_write_never_serializes_the_full_manifest_dict_at_once(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex round-6 HIGH finding #8: _write_content_manifest built a
        full point_id -> digest dict in memory, one entry per point_id,
        then serialized the WHOLE dict in one json.dump call -- for
        millions of chunks this is real, unbounded memory growth. Fix:
        stream each entry directly to the temp file as it's computed,
        never holding/serializing more than a small bounded number of
        entries at once. A small threshold (5) comfortably separates a
        genuine O(n=50) manifest dict from any incidental small
        bookkeeping dict (e.g. the 1-key chunks_db discriminator) also
        written during a real consolidation pass."""
        import json as _json_module

        _write_collection_meta(tmp_path)
        for i in range(50):
            _write_vector_json(
                tmp_path,
                f"nn{i:06d}",
                [1.0, 2.0, 3.0, 4.0],
                chunk_text=f"content-{i}",
            )

        max_dict_size_dumped = {"value": 0}
        original_dump = _json_module.dump

        def _tracking_dump(obj, fp, *args, **kwargs):
            if isinstance(obj, dict):
                max_dict_size_dumped["value"] = max(
                    max_dict_size_dumped["value"], len(obj)
                )
            return original_dump(obj, fp, *args, **kwargs)

        monkeypatch.setattr(_json_module, "dump", _tracking_dump)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        assert max_dict_size_dumped["value"] <= 5, (
            f"Bug: a single json.dump call serialized a dict with "
            f"{max_dict_size_dumped['value']} entries -- the full "
            f"manifest (or an equivalently large structure) was "
            f"materialized in memory before writing, instead of "
            f"streaming one entry at a time."
        )

        # Correctness is unaffected -- the manifest is still complete
        # and readable.
        manifest = _json_module.loads(
            (tmp_path / "chunks_db_content_manifest.json").read_text()
        )
        assert len(manifest["records"]) == 50

    def test_post_cleanup_content_corruption_is_detected_and_raises(
        self, tmp_path: Path
    ) -> None:
        """The CRITICAL scenario: legacy source is ALREADY GONE (cleanup
        fully completed), then chunks.db's stored content is corrupted
        (valid key, wrong payload/vector) -- e.g. bit rot, a bad manual
        edit. Key-presence-only verification would miss this forever;
        content-manifest verification must catch it and refuse to
        silently treat the collection as still-good."""
        _write_collection_meta(tmp_path)
        _write_vector_json(
            tmp_path, "mm003333", [1.0, 2.0, 3.0, 4.0], chunk_text="original"
        )

        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "consolidated"
        assert not (tmp_path / "vector_mm003333.json").exists()  # legacy gone

        # Simulate post-migration corruption: overwrite the stored record's
        # content in place (same id, different payload) -- INSERT OR
        # REPLACE, exactly what silent on-disk corruption would produce.
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "mm003333",
                        "vector": [9.0, 9.0, 9.0, 9.0],
                        "metadata": {},
                        "payload": {"path": "src/foo.py", "language": "python"},
                        "chunk_text": "CORRUPTED",
                        "indexed_with_uncommitted_changes": True,
                    }
                ]
            )

        # Bug #1486: this record has NO remaining legacy source, so a
        # content-digest mismatch here is UNRECOVERABLE, not a bare
        # retryable verification failure.
        from code_indexer.storage.shared.collection_migration import (
            UnrecoverableConsolidationCorruptionError,
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_digest_detects_corruption_of_a_middle_element_in_a_realistic_dimension_vector(
        self, tmp_path: Path
    ) -> None:
        """Codex CRITICAL finding (round 5): json.dumps(..., default=str)
        on a NumPy array calls str(ndarray), which for arrays past
        NumPy's print-summarization threshold (a 1024-dim embedding
        vector qualifies) produces a TRUNCATED repr with an ellipsis
        (e.g. '[0.1, 0.2, ..., 0.9]') -- interior-element corruption is
        completely undetectable. A 4-element toy vector never hits this
        threshold, which is why the earlier corruption test passed
        without proving anything about real embedding dimensions."""
        _write_collection_meta(tmp_path)
        dim = 1024
        original_vector = [float(i) / 1000.0 for i in range(dim)]
        _write_vector_json(tmp_path, "pp001111", original_vector, chunk_text="original")

        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "consolidated"

        # Corrupt ONLY element 500 -- deep inside NumPy's truncated middle
        # region for a 1024-element array -- everything else identical.
        corrupted_vector = list(original_vector)
        corrupted_vector[500] = corrupted_vector[500] + 100.0
        with ChunkStore(tmp_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "pp001111",
                        "vector": corrupted_vector,
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/foo.py", "language": "python"},
                        "chunk_text": "original",
                        "indexed_with_uncommitted_changes": True,
                    }
                ]
            )

        # Bug #1486: this record has NO remaining legacy source, so a
        # content-digest mismatch here is UNRECOVERABLE, not a bare
        # retryable verification failure.
        from code_indexer.storage.shared.collection_migration import (
            UnrecoverableConsolidationCorruptionError,
        )

        with pytest.raises(UnrecoverableConsolidationCorruptionError):
            consolidate_collection_in_place(tmp_path)

    def test_detects_a_row_silently_deleted_from_chunks_db(
        self, tmp_path: Path
    ) -> None:
        """Codex CRITICAL finding (round 5): per-key digest verification
        only iterates over ids CURRENTLY present in chunks.db -- a row
        that vanishes entirely (deleted) is simply absent from that
        iteration, so its disappearance goes completely unnoticed unless
        the manifest's own key SET is compared against chunks.db's actual
        row-id set."""
        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "qq001111", [1.0, 2.0, 3.0, 4.0], chunk_text="a")
        _write_vector_json(tmp_path, "qq002222", [5.0, 6.0, 7.0, 8.0], chunk_text="b")

        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "consolidated"
        assert verify_collection_fully_migrated(tmp_path) is True

        # Silently delete ONE row -- the legacy source is long gone, so
        # this is unrecoverable and must be detected.
        with ChunkStore(tmp_path / "chunks.db") as store:
            deleted_count = store.delete(["qq002222"])
        assert deleted_count == 1

        assert verify_collection_fully_migrated(tmp_path) is False, (
            "Bug: a row silently deleted from chunks.db (with its legacy "
            "source long gone) was NOT detected -- verification only "
            "checked ids CURRENTLY present, never the manifest's full "
            "key set."
        )

    def test_detects_manifest_file_deleted_entirely(self, tmp_path: Path) -> None:
        """Codex CRITICAL finding (round 5): if ALL still-present-legacy
        coverage is gone AND the manifest file itself is deleted, there
        is nothing left in the 'currently present but wrong' iteration to
        flag -- the collection must still be reported as NOT verified,
        never silently trusted."""
        from code_indexer.storage.shared.collection_migration import (
            verify_collection_fully_migrated,
        )

        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "qq003333", [1.0, 2.0, 3.0, 4.0], chunk_text="a")

        result = consolidate_collection_in_place(tmp_path)
        assert result.status == "consolidated"
        assert verify_collection_fully_migrated(tmp_path) is True

        (tmp_path / "chunks_db_content_manifest.json").unlink()

        assert verify_collection_fully_migrated(tmp_path) is False, (
            "Bug: the content-integrity manifest was deleted entirely, "
            "but the collection still reported as fully verified -- the "
            "chunks_db discriminator being set does not itself prove "
            "content integrity without the manifest."
        )


class TestCleanupTOCTOUProbeRemoved:
    """Codex MEDIUM finding (round 4): the improved cleanup handler still
    caught EVERY OSError from unlink() and did a SECOND fallible
    Path.exists() probe to decide if it was a harmless race -- but that
    probe can itself raise (e.g. ELOOP) or return a false negative, so it
    is not proof of ENOENT. Fix: only a FileNotFoundError raised DIRECTLY
    by unlink() itself is the benign race case; every other OSError is a
    real failure, no second probe needed."""

    def test_cleanup_treats_non_file_not_found_oserror_as_failure_without_a_second_exists_probe(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationCleanupError,
        )

        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "nn001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x"
        )

        original_unlink = Path.unlink
        original_exists = Path.exists
        exists_probed_for_vfile = {"called": False}

        def _failing_unlink(self, *args, **kwargs):
            if self == vfile:
                raise PermissionError(f"simulated permission denied for {self}")
            return original_unlink(self, *args, **kwargs)

        def _tracking_exists(self, *args, **kwargs):
            if self == vfile:
                # A second .exists() probe on the file that just failed to
                # unlink is EXACTLY the fallible TOCTOU check this fix
                # removes -- fail loudly if it's ever called, rather than
                # merely returning an unreliable answer.
                exists_probed_for_vfile["called"] = True
                raise AssertionError(
                    "Bug: cleanup called a second, fallible .exists() "
                    "probe on the file that just failed to unlink -- "
                    "only a direct FileNotFoundError from unlink() itself "
                    "may be treated as a benign race."
                )
            return original_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _failing_unlink)
        monkeypatch.setattr(Path, "exists", _tracking_exists)

        # Must raise the EXPECTED ConsolidationCleanupError -- never the
        # AssertionError from a second .exists() probe that this fix
        # removes entirely.
        with pytest.raises(ConsolidationCleanupError):
            consolidate_collection_in_place(tmp_path)

        assert exists_probed_for_vfile["called"] is False

    def test_cleanup_treats_direct_file_not_found_error_from_unlink_as_harmless_race(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        vfile = _write_vector_json(
            tmp_path, "nn002222", [5.0, 6.0, 7.0, 8.0], chunk_text="y"
        )

        original_unlink = Path.unlink

        def _vanished_unlink(self, *args, **kwargs):
            if self == vfile:
                raise FileNotFoundError(f"simulated race: {self} already gone")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _vanished_unlink)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"

    def test_id_index_bin_unlink_never_gated_behind_a_fallible_exists_probe(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex MEDIUM finding (round 5): unlike the stray vector_*.json
        loop above, the id_index.bin cleanup still gated its FIRST unlink
        attempt behind ``if id_index_bin.exists(): try: unlink() ...`` --
        the exact fallible TOCTOU pre-check round 4's fix removed from
        the sibling loop. A false-negative .exists() (e.g. a transient
        stat error swallowed upstream, or a race where the file appears
        between the check and the unlink) would silently skip a real
        stale id_index.bin instead of unlinking it. Fix: attempt
        unlink() unconditionally, treating a direct FileNotFoundError
        from unlink() itself as the only legitimate 'already gone' case
        -- never a separate .exists() probe."""
        from code_indexer.storage.id_index_manager import IDIndexManager

        _write_collection_meta(tmp_path)
        _write_vector_json(tmp_path, "pp001111", [1.0, 2.0, 3.0, 4.0], chunk_text="x")
        id_index_bin = tmp_path / IDIndexManager.INDEX_FILENAME
        id_index_bin.write_bytes(b"stale-legacy-index")

        original_exists = Path.exists

        def _tracking_exists(self, *args, **kwargs):
            if self == id_index_bin:
                raise AssertionError(
                    "Bug: cleanup called a fallible .exists() probe to "
                    "decide whether to unlink id_index.bin -- unlink() "
                    "must be attempted unconditionally, with only a "
                    "direct FileNotFoundError from unlink() itself "
                    "treated as a benign race."
                )
            return original_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", _tracking_exists)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "consolidated"
        # os.path.exists (not the monkeypatched Path.exists) -- this
        # assertion must not itself trip the guard above.
        assert not os.path.exists(str(id_index_bin))


class TestVerifyRecordFieldForFieldDetectsDroppedNullableField:
    """Codex round-6 CRITICAL finding #1: ``stored.get(key) !=
    expected_value`` cannot distinguish a key that is genuinely ``None``
    from a key that is MISSING entirely -- both make ``stored.get(key)``
    return ``None``, so a silently dropped field is invisible to
    verification. Real repro: a record corrupted by losing a nullable
    field passed verification, its only legacy source was then deleted
    by cleanup, and the corruption was undetectable until the NEXT
    invocation -- by which point the source is already gone and the lost
    field can never be recovered."""

    def test_missing_key_with_none_original_value_is_detected_directly(self) -> None:
        from code_indexer.storage.shared.collection_migration import (
            ConsolidationVerificationError,
            _verify_record_field_for_field,
        )

        original = {
            "id": "nullable1",
            "vector": [1.0, 2.0, 3.0, 4.0],
            "chunk_text": "only intact source",
            "load_bearing_nullable_field": None,
        }
        # The stored record is missing the key ENTIRELY -- not present
        # with value None, genuinely absent (simulating field-drop
        # corruption).
        stored = {
            "id": "nullable1",
            "vector": [1.0, 2.0, 3.0, 4.0],
            "chunk_text": "only intact source",
        }

        with pytest.raises(ConsolidationVerificationError):
            _verify_record_field_for_field("nullable1", original, stored)

    def test_resume_self_heals_a_dropped_nullable_field_while_legacy_source_intact(
        self, tmp_path: Path
    ) -> None:
        """End-to-end mirror of Codex's real repro: chunks.db is
        hand-constructed to simulate the exact crash point where a
        record's nullable field was dropped (corrupted), the legacy
        source is STILL on disk, and the discriminator is already set
        (a genuine 'resume' call) -- verification must catch the
        corruption and trigger a self-heal rebuild, never silently pass
        and let cleanup delete the only remaining correct source."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        _write_collection_meta(tmp_path)
        legacy = _write_vector_json(
            tmp_path,
            "nullable1",
            [1.0, 2.0, 3.0, 4.0],
            chunk_text="only intact source",
        )
        legacy_record = json.loads(legacy.read_text())
        legacy_record["load_bearing_nullable_field"] = None
        legacy.write_text(json.dumps(legacy_record))

        # chunks.db has the record, but the nullable field was DROPPED
        # (corruption) relative to the still-present legacy source.
        corrupted_stored = dict(legacy_record)
        del corrupted_stored["load_bearing_nullable_field"]
        db_path = tmp_path / "chunks.db"
        with ChunkStore(db_path) as store:
            store.write_batch([corrupted_stored])

        # A real manifest from the original fresh migration would reflect
        # the CORRECT (undropped) content, never the corrupted stored
        # state being simulated here.
        _write_manifest_for_records(tmp_path, {"nullable1": legacy_record})
        write_chunks_db_discriminator(tmp_path)

        result = consolidate_collection_in_place(tmp_path)

        assert result.status == "already_consolidated"
        assert not legacy.exists()
        with ChunkStore(db_path, immutable=True) as store:
            stored = store.read("nullable1")
        assert "load_bearing_nullable_field" in stored, (
            "Bug: the dropped nullable field was never detected/repaired "
            "-- corruption passed verification silently while the "
            "legacy source was still available to self-heal from."
        )
        assert stored["load_bearing_nullable_field"] is None
