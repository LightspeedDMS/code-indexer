"""Unit tests for Bug #1969 Round 4, finding R3-F1 (P2 BLOCKING):
IDIndexManager.rebuild_from_vectors() must surface which relative file
paths a self-heal escalation wiped, on the SAME instance the caller
already holds a reference to -- so FilesystemVectorStore._load_id_index()
can read it off `index_manager` right after the call, with no return-type
change needed for rebuild_from_vectors() itself (still Dict[str, Path]).
"""

import hashlib
import json
from pathlib import Path

from code_indexer.storage.id_index_manager import IDIndexManager


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


class TestRebuildFromVectorsExposesLastSelfHealWipedPaths:
    def test_escalated_self_heal_exposes_wiped_relative_paths(
        self, tmp_path: Path
    ) -> None:
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:exposecase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/exposed.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:exposecase",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/exposed.py",
        )
        _corrupt_id_index(tmp_path)

        manager = IDIndexManager()
        manager.rebuild_from_vectors(tmp_path, self_heal=True)

        assert manager.last_self_heal_wiped_relative_paths == frozenset(
            {"src/exposed.py"}
        )

    def test_fresh_instance_defaults_to_empty(self, tmp_path: Path) -> None:
        manager = IDIndexManager()
        assert manager.last_self_heal_wiped_relative_paths == frozenset()

    def test_normal_non_escalated_repair_leaves_it_empty(self, tmp_path: Path) -> None:
        """A repairable duplicate (valid id_index.bin naming a winner) is
        resolved by the ORDINARY repair path, never the escalation -- must
        NOT populate last_self_heal_wiped_relative_paths."""
        _write_collection_meta(tmp_path)
        winner_path = _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ordinarycase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/ordinary.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:ordinarycase",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/ordinary.py",
        )
        shared_point_id = _point_id("proj", "sha256:ordinarycase", 0)
        IDIndexManager().save_index(tmp_path, {shared_point_id: winner_path})

        manager = IDIndexManager()
        manager.rebuild_from_vectors(tmp_path, self_heal=True)

        assert manager.last_self_heal_wiped_relative_paths == frozenset()

    def test_reused_instance_resets_between_calls(self, tmp_path: Path) -> None:
        """A single IDIndexManager instance reused across two calls must
        not leak a PRIOR call's wiped paths into a later call that had
        nothing to wipe."""
        _write_collection_meta(tmp_path)
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:resetcase",
            index=0,
            total_chunks=1,
            vector=[0.1, 0.2, 0.3, 0.4],
            line_start=0,
            line_end=9,
            shard_suffix="-a",
            path="src/reset_me.py",
        )
        _write_record(
            tmp_path,
            project_id="proj",
            file_hash="sha256:resetcase",
            index=0,
            total_chunks=1,
            vector=[0.9, 0.9, 0.9, 0.9],
            line_start=0,
            line_end=9,
            shard_suffix="-b",
            path="src/reset_me.py",
        )
        _corrupt_id_index(tmp_path)

        manager = IDIndexManager()
        manager.rebuild_from_vectors(tmp_path, self_heal=True)
        assert manager.last_self_heal_wiped_relative_paths == frozenset(
            {"src/reset_me.py"}
        )

        # Second call against the now-clean collection: nothing to wipe.
        manager.rebuild_from_vectors(tmp_path, self_heal=True)
        assert manager.last_self_heal_wiped_relative_paths == frozenset()
