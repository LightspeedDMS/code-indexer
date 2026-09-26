"""Unit tests for Bug #1969 Round 4, finding R3-F1 (P2 BLOCKING): the
whole-file-wipe recovery must surface WHICH relative file paths it wiped,
so the indexing orchestrator (smart_indexer.py) can guarantee those files
are reprocessed within the SAME run rather than silently left
permanently unsearchable.

Round 3's `recover_from_corrupt_id_index_by_wiping_files()` deleted the
implicated files' chunks correctly but threw away the information a
caller would need to re-queue them -- its `DuplicateFileWipeResult` only
carried counts (`file_hashes_wiped`, `records_deleted`). This module
proves the function now ALSO returns the actual relative paths wiped, via
a new `wiped_relative_paths` field, sourced from each deleted record's
`payload.path` (captured by `_extract_lightweight_identity_fields`, which
did not previously retain the path VALUE -- only a `has_path` presence
boolean, Bug #1558).
"""

import hashlib
import json
from pathlib import Path

from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    recover_from_corrupt_id_index_by_wiping_files,
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


class TestWipeResultSurfacesRelativePaths:
    def test_returns_the_relative_path_of_each_wiped_file(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        # File A: a genuine duplicate -- must be wiped, path surfaced.
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:pathcheck_a",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/collided_a.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:pathcheck_a",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/collided_a.py",
        )
        # File B: unrelated -- must NOT appear in wiped_relative_paths.
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:pathcheck_b",
            index=0,
            total_chunks=1,
            vector=[0.5, 0.5, 0.5, 0.5],
            line_start=0,
            line_end=5,
            shard_suffix="-a",
            path="src/unaffected_b.py",
        )
        _corrupt_id_index(tmp_path)

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert result.wiped_relative_paths == frozenset({"src/collided_a.py"})

    def test_multiple_implicated_files_all_surfaced(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        for file_hash, path in (
            ("sha256:multi_1", "src/one.py"),
            ("sha256:multi_2", "src/two.py"),
        ):
            for suffix, vec in (
                ("-a", [0.1, 0.1, 0.1, 0.1]),
                ("-b", [0.9, 0.9, 0.9, 0.9]),
            ):
                _write_record(
                    tmp_path,
                    project_id="proj",
                    file_hash=file_hash,
                    index=0,
                    total_chunks=1,
                    vector=vec,
                    line_start=0,
                    line_end=5,
                    shard_suffix=suffix,
                    path=path,
                )
        _corrupt_id_index(tmp_path)

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert result.wiped_relative_paths == frozenset({"src/one.py", "src/two.py"})

    def test_no_duplicates_returns_empty_paths(self, tmp_path: Path) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:clean_path",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/clean.py",
        )

        result = recover_from_corrupt_id_index_by_wiping_files(tmp_path)

        assert result.wiped_relative_paths == frozenset()
