"""Unit tests for Bug #1969 Round 3: the REAL corrupt-index self-heal.

Round 2 made DuplicateSourceIdError self-healable via
repair_duplicate_and_shifted_points(), but the second reviewer found that
in the EXACT bug-report scenario -- a corrupt/unreadable id_index.bin --
that repair's own AC30 safety rule (_plan_dedup) can never pick a winner,
because it needs to read the very id_index.bin that is corrupt to know
"who is currently being served". It raises DedupRepairAmbiguousError
instead: a better-labeled hard failure, not a silent recovery.

This module tests the two building blocks of the REAL fix:

1. DedupRepairAmbiguousError now carries a `reason` attribute (a
   DedupRepairAmbiguousReason enum member) set distinctly at each of this
   module's 9 raise sites, so a caller can tell PROGRAMMATICALLY (never by
   parsing message text) which one fired. Only
   DedupRepairAmbiguousReason.CORRUPT_ID_INDEX (the id_index.bin-failed-
   to-load case) is safe for automatic escalation -- every other reason
   signals data whose identity assumptions this repair cannot trust, and
   must keep propagating as a hard failure requiring manual review.

2. recover_from_corrupt_id_index_by_wiping_files(): given a collection
   whose id_index.bin is corrupt/unreadable, deletes EVERY vector_*.json
   record belonging to EVERY file implicated by ANY duplicate point_id
   group (the whole file, not just the colliding chunks -- so the file
   ends up fully MISSING rather than half-indexed, which is the safe
   state for smart_indexer.py's reconcile), then rebuilds id_index.bin/
   HNSW from the remaining, now conflict-free records. Metadata-only,
   applies the same .versioned/ snapshot guard as the rest of this
   module.

IDIndexManager.rebuild_from_vectors()'s wiring of this escalation is
tested separately in
tests/unit/storage/test_id_index_manager_1969_round3_real_self_heal.py.
"""

import hashlib
import json
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import DuplicateSourceIdError, IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
    DedupRepairAmbiguousReason,
    DuplicateFileWipeResult,
    recover_from_corrupt_id_index_by_wiping_files,
    repair_duplicate_and_shifted_points,
)


def _point_id(project_id: str, file_hash: str, index: int) -> str:
    return hashlib.md5(f"{project_id}_{file_hash}_{index}".encode()).hexdigest()


def _write_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    total_chunks: int,
    vector: list,
    line_start,
    line_end,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    """Write one legacy sharded vector_<id>.json record using the REAL
    production identity scheme (unique_key = f"{project_id}_{file_hash}_
    {index}", point_id = md5(unique_key)) -- self-consistent with this
    module's whole-collection identity gate."""
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = _point_id(project_id, file_hash, index)
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": total_chunks,
        "line_start": line_start,
        "line_end": line_end,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _write_collection_meta(
    collection_dir: Path, *, vector_dim: int = 4, space: str = "cosine"
) -> None:
    meta = {
        "name": "coll",
        "vector_size": vector_dim,
        "hnsw_index": {
            "version": 1,
            "vector_dim": vector_dim,
            "space": space,
            "vector_count": 0,
            "id_mapping": {},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(meta))


def _corrupt_id_index(collection_dir: Path) -> None:
    """Write a truncated/corrupt id_index.bin -- fails CorruptIDIndexError
    on load (mirrors the exact bug-report shape: id_index.bin itself
    cannot be loaded at all)."""
    (collection_dir / IDIndexManager.INDEX_FILENAME).write_bytes(b"\x05\x00\x00")


class TestDedupRepairAmbiguousReasonAttribute:
    """Item 1: DedupRepairAmbiguousError.reason distinguishes WHY it was
    raised, set distinctly per raise site."""

    def test_corrupt_id_index_reason(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:aaa",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:aaa",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        _corrupt_id_index(tmp_path)

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert exc_info.value.reason == DedupRepairAmbiguousReason.CORRUPT_ID_INDEX

    def test_malformed_records_reason(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:bbb",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        (tmp_path / "vector_malformed.json").write_text(json.dumps({"vector": [0.5]}))

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert exc_info.value.reason == DedupRepairAmbiguousReason.MALFORMED_RECORDS

    def test_stale_marker_empty_tree_reason(self, tmp_path: Path) -> None:
        (tmp_path / ".dedup-repair-pending").write_text(
            json.dumps({"pending_since": 1.0})
        )

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert (
            exc_info.value.reason == DedupRepairAmbiguousReason.STALE_MARKER_EMPTY_TREE
        )

    def test_mixed_line_start_presence_reason(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:mixed",
            index=0,
            total_chunks=2,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:mixed",
            index=1,
            total_chunks=2,
            vector=[0.5, 0.5, 0.5, 0.5],
            line_start=None,
            line_end=None,
            shard_suffix="-b",
        )

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert (
            exc_info.value.reason
            == DedupRepairAmbiguousReason.MIXED_LINE_START_PRESENCE
        )

    def test_hnsw_params_meta_unreadable_reason(self, tmp_path: Path) -> None:
        # No collection_meta.json at all -- and a single record whose
        # old_index (5) differs from its canonical renumbered index (0),
        # so a renumber IS required and HNSW params must be resolved.
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:nometa",
            index=5,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert (
            exc_info.value.reason
            == DedupRepairAmbiguousReason.HNSW_PARAMS_META_UNREADABLE
        )

    def test_hnsw_params_vector_dim_undeterminable_reason(self, tmp_path: Path) -> None:
        (tmp_path / "collection_meta.json").write_text(
            json.dumps({"name": "coll", "hnsw_index": {"space": "cosine"}})
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:nodim",
            index=5,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            repair_duplicate_and_shifted_points(tmp_path)

        assert (
            exc_info.value.reason
            == DedupRepairAmbiguousReason.HNSW_PARAMS_VECTOR_DIM_UNDETERMINABLE
        )


class TestRecoverFromCorruptIdIndexByWipingFiles:
    """Item 2: the new whole-file-wipe escalation function itself."""

    def test_wipes_all_chunks_of_implicated_file_and_rebuilds(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        # File A: a genuine duplicate on chunk_index=2, PLUS an unrelated
        # third chunk (index=0) of the SAME file -- must ALSO be wiped
        # (whole-file wipe, not just the colliding pair).
        colliding_1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:filea",
            index=2,
            total_chunks=3,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=20,
            line_end=29,
            shard_suffix="-a",
            path="src/a.py",
        )
        colliding_2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:filea",
            index=2,
            total_chunks=3,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=20,
            line_end=29,
            shard_suffix="-b",
            path="src/a.py",
        )
        untouched_sibling_chunk = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:filea",
            index=0,
            total_chunks=3,
            vector=[0.2, 0.2, 0.2, 0.2],
            line_start=0,
            line_end=9,
            shard_suffix="-c",
            path="src/a.py",
        )
        # File B: completely unrelated, no collision -- must survive
        # untouched.
        file_b_chunk = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:fileb",
            index=0,
            total_chunks=1,
            vector=[0.3, 0.3, 0.3, 0.3],
            line_start=0,
            line_end=5,
            shard_suffix="-a",
            path="src/b.py",
        )
        _corrupt_id_index(tmp_path)

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert isinstance(result, DuplicateFileWipeResult)
        assert result.file_hashes_wiped == 1
        assert result.records_deleted == 3
        assert not colliding_1.exists()
        assert not colliding_2.exists()
        assert not untouched_sibling_chunk.exists()
        assert file_b_chunk.exists()

        # id_index.bin was rebuilt (no longer corrupt) and has zero
        # entries for file A's file_hash -- only file B survives.
        rebuilt_index = IDIndexManager().load_index(tmp_path)
        assert len(rebuilt_index) == 1
        surviving_path = next(iter(rebuilt_index.values()))
        assert surviving_path.resolve() == file_b_chunk.resolve()

        # Zero points remain with payload.path == "src/a.py" -- exactly
        # the post-condition smart_indexer.py's reconcile needs to treat
        # the file as fully missing (not half-indexed) and reprocess it.
        remaining_paths_for_a = [
            json.loads(p.read_text())["payload"]["path"]
            for p in tmp_path.rglob("vector_*.json")
            if json.loads(p.read_text()).get("payload", {}).get("path") == "src/a.py"
        ]
        assert remaining_paths_for_a == []

    def test_refuses_to_mutate_versioned_snapshot(self, tmp_path: Path) -> None:
        snapshot_dir = (
            tmp_path
            / ".versioned"
            / "my-golden-repo"
            / "v_1700000000"
            / ".code-indexer"
            / "index"
            / "voyage-code-3"
        )
        snapshot_dir.mkdir(parents=True)
        _write_collection_meta(snapshot_dir)
        winner = _write_record(
            snapshot_dir,
            project_id="proj",
            file_hash="sha256:versioned",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        loser = _write_record(
            snapshot_dir,
            project_id="proj",
            file_hash="sha256:versioned",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        _corrupt_id_index(snapshot_dir)

        with pytest.raises(DuplicateSourceIdError):
            recover_from_corrupt_id_index_by_wiping_files(snapshot_dir)

        assert winner.exists()
        assert loser.exists()

    def test_no_deletion_when_hnsw_params_undeterminable(self, tmp_path: Path) -> None:
        """Mirrors repair_duplicate_and_shifted_points's own established
        pattern (Codex LOW finding 5): the authoritative HNSW build
        parameters must be resolved BEFORE any mutation -- an
        undeterminable value must fail loud, pre-mutation, never after
        files have already been deleted."""
        # Deliberately NO collection_meta.json -- HNSW params cannot be
        # resolved.
        colliding_1 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:nometa2",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )
        colliding_2 = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:nometa2",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=1,
            line_end=10,
            shard_suffix="-b",
        )
        _corrupt_id_index(tmp_path)

        with pytest.raises(DedupRepairAmbiguousError) as exc_info:
            recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert (
            exc_info.value.reason
            == DedupRepairAmbiguousReason.HNSW_PARAMS_META_UNREADABLE
        )
        # Zero mutation: both colliding records survive untouched.
        assert colliding_1.exists()
        assert colliding_2.exists()

    def test_no_duplicates_is_a_clean_noop(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        chunk = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:clean",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=1,
            line_end=10,
            shard_suffix="-a",
        )

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert result.file_hashes_wiped == 0
        assert result.records_deleted == 0
        assert chunk.exists()
