"""Unit tests for Bug #1969 Round 5, finding R4-F1 (P2 BLOCKING): the
round-4 in-memory `_pending_self_heal_reprocess_paths` queue is drained
by only ONE code path (`_do_incremental_index`'s post-pass drain) and is
lost on process exit -- several OTHER trigger sites (end_indexing's
finally block, upsert_points during resume, scroll_points under
detect_deletions) wipe a file's chunks and NEVER surface the paths
anywhere, leaving the file permanently unsearchable.

This module tests the durable replacement: a fsynced sidecar file
(mirroring this module's own `.dedup-repair-pending` / `.dedup-outcome-
pending` pattern) recorded at the SINGLE choke point every self-heal
call site already funnels through --
`recover_from_corrupt_id_index_by_wiping_files()` -- BEFORE any deletion,
so recording is guaranteed regardless of which caller triggered the wipe,
and survives a process crash/restart.
"""

import hashlib
import json
from pathlib import Path

import pytest

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    clear_self_heal_reprocess_paths,
    read_pending_self_heal_reprocess_paths,
    record_self_heal_reprocess_pending,
    recover_from_corrupt_id_index_by_wiping_files,
    SELF_HEAL_REPROCESS_PENDING_FILENAME,
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
    path: str,
) -> Path:
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
    (collection_dir / IDIndexManager.INDEX_FILENAME).write_bytes(b"\x05\x00\x00")


class TestDurableSidecarPrimitives:
    def test_read_absent_returns_empty(self, tmp_path: Path) -> None:
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset()

    def test_record_and_read_roundtrip(self, tmp_path: Path) -> None:
        record_self_heal_reprocess_pending(tmp_path, frozenset({"a.py", "b.py"}))
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
            {"a.py", "b.py"}
        )
        assert (tmp_path / SELF_HEAL_REPROCESS_PENDING_FILENAME).exists()

    def test_record_merges_with_existing_rather_than_overwriting(
        self, tmp_path: Path
    ) -> None:
        record_self_heal_reprocess_pending(tmp_path, frozenset({"a.py"}))
        record_self_heal_reprocess_pending(tmp_path, frozenset({"b.py"}))
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
            {"a.py", "b.py"}
        )

    def test_clear_removes_only_specified_paths(self, tmp_path: Path) -> None:
        record_self_heal_reprocess_pending(
            tmp_path, frozenset({"a.py", "b.py", "c.py"})
        )
        clear_self_heal_reprocess_paths(tmp_path, ["b.py"])
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
            {"a.py", "c.py"}
        )

    def test_clear_all_deletes_sidecar_file(self, tmp_path: Path) -> None:
        record_self_heal_reprocess_pending(tmp_path, frozenset({"a.py"}))
        clear_self_heal_reprocess_paths(tmp_path, ["a.py"])
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset()
        assert not (tmp_path / SELF_HEAL_REPROCESS_PENDING_FILENAME).exists()

    def test_clear_nonexistent_sidecar_is_a_safe_noop(self, tmp_path: Path) -> None:
        clear_self_heal_reprocess_paths(tmp_path, ["never-recorded.py"])
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset()

    def test_clear_unknown_path_leaves_others_untouched(self, tmp_path: Path) -> None:
        record_self_heal_reprocess_pending(tmp_path, frozenset({"a.py"}))
        clear_self_heal_reprocess_paths(tmp_path, ["never-recorded.py"])
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset({"a.py"})


class TestRecoveryFunctionRecordsDurablyBeforeDeletion:
    """The critical crash-safety property: the durable sidecar must be
    written BEFORE the first physical deletion, so a crash mid-wipe (or
    mid-HNSW-rebuild, the long-lock-hold window R3-F5 flagged) still
    leaves the record intact for the NEXT run to pick up."""

    def test_sidecar_present_even_if_deletion_crashes_immediately_after_write(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crashcase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/crash_target.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:crashcase",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/crash_target.py",
        )
        _corrupt_id_index(tmp_path)

        import code_indexer.storage.shared.collection_dedup_repair as repair_mod

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash during deletion")

        monkeypatch.setattr(repair_mod, "_delete_loser", _boom)

        with pytest.raises(RuntimeError, match="simulated crash"):
            recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        # Despite the crash, the durable sidecar was already written
        # BEFORE the first deletion attempt.
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
            {"src/crash_target.py"}
        )

    def test_successful_wipe_records_durably_and_result_still_matches(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:normalcase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/normal.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:normalcase",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/normal.py",
        )
        _corrupt_id_index(tmp_path)

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert result.wiped_relative_paths == frozenset({"src/normal.py"})
        assert read_pending_self_heal_reprocess_paths(tmp_path) == frozenset(
            {"src/normal.py"}
        )
