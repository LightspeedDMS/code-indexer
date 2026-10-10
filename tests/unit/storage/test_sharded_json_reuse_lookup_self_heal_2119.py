"""Story #2119 x Bug #1969: on a legacy SHARDED_JSON collection the
embedding-reuse lookup is a write-path caller (it runs only inside an
indexing run, right before the file's upsert). A duplicated point_id behind
a corrupt ``id_index.bin`` is the known, bounded Bug #1969 self-healing
condition, not a lookup failure: the lookup runs the same opt-in self-heal
the upsert runs (wipe the implicated files, record them for same-run
reprocess). Every genuine error still propagates (never a silent cache miss
that pays). Real ``FilesystemVectorStore``, real files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.collection_dedup_repair import (
    read_pending_self_heal_reprocess_paths,
)

VECTOR_DIM = 16
COLLECTION = "voyage-code-3"
PROJECT = "example-repo"


VICTIM_HASH = "sha256:" + hashlib.sha256(b"victim file").hexdigest()
TARGET_HASH = "sha256:" + hashlib.sha256(b"target file").hexdigest()


def _unique_key(file_hash: str, index: int) -> str:
    return f"{PROJECT}_{file_hash}_{index}"


def _points(path: str, file_hash: str, texts: List[str]) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(len(texts) + len(path))
    return [
        {
            "id": hashlib.md5(_unique_key(file_hash, i).encode()).hexdigest(),
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {
                "path": path,
                "type": "content",
                "unique_key": _unique_key(file_hash, i),
                "chunk_index": i,
                "total_chunks": len(texts),
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "content": text,
                "file_hash": file_hash,
            },
        }
        for i, text in enumerate(texts)
    ]


def _sharded_collection(base: Path) -> Path:
    store = FilesystemVectorStore(
        base_path=base, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points("victim.py", VICTIM_HASH, ["v0"]))
    store.upsert_points(COLLECTION, _points("target.py", TARGET_HASH, ["t0"]))
    store.end_indexing(COLLECTION)
    return base / COLLECTION


def _record_files(collection_path: Path, rel_path: str) -> List[Path]:
    return [
        f
        for f in collection_path.rglob("vector_*.json")
        if json.loads(f.read_text()).get("payload", {}).get("path") == rel_path
    ]


def _lookup(base: Path, path: str, file_hash: str) -> Dict[str, Any]:
    store = FilesystemVectorStore(
        base_path=base, use_chunks_db_for_new_collections=False
    )
    return store.get_existing_content_hashes(
        path, COLLECTION, project_id=PROJECT, file_hash=file_hash, chunk_count=1
    )


def _duplicate_victim_and_corrupt_index(collection_path: Path) -> None:
    """The Bug #1969 shape: victim.py's points duplicated (same id, other
    content) in a sibling directory, behind a corrupt id_index.bin."""
    for record_file in _record_files(collection_path, "victim.py"):
        record = json.loads(record_file.read_text())
        record["vector"] = [-v for v in record["vector"]]
        dup_dir = collection_path / "dup_test" / record["id"][:4]
        dup_dir.mkdir(parents=True)
        (dup_dir / record_file.name).write_text(json.dumps(record))
    (collection_path / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def test_lookup_never_self_heals_an_immutable_versioned_snapshot(
    tmp_path: Path,
) -> None:
    """The lookup's opt-in self-heal goes through rebuild_from_vectors,
    which refuses to mutate an immutable ``.versioned/{alias}/v_{ts}``
    snapshot: the original error propagates and nothing is touched."""
    from code_indexer.storage.id_index_manager import DuplicateSourceIdError

    base = tmp_path / ".versioned" / "example-repo-global" / "v_1700000000" / "index"
    collection_path = _sharded_collection(base)
    _duplicate_victim_and_corrupt_index(collection_path)
    files_before = sorted(collection_path.rglob("vector_*.json"))

    with pytest.raises(DuplicateSourceIdError):
        _lookup(base, "target.py", TARGET_HASH)

    assert sorted(collection_path.rglob("vector_*.json")) == files_before
    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset()


def test_duplicate_point_id_behind_a_corrupt_id_index_self_heals_in_the_lookup(
    tmp_path: Path,
) -> None:
    collection_path = _sharded_collection(tmp_path)
    _duplicate_victim_and_corrupt_index(collection_path)

    result = _lookup(tmp_path, "target.py", TARGET_HASH)

    assert set(result) == {hashlib.sha256(b"t0").hexdigest()}
    assert "victim.py" in read_pending_self_heal_reprocess_paths(collection_path), (
        "the wiped file must be recorded for same-run reprocess"
    )


def test_a_malformed_record_still_fails_the_lookup(tmp_path: Path) -> None:
    collection_path = _sharded_collection(tmp_path)
    for record_file in _record_files(collection_path, "target.py"):
        record_file.write_text("[]")  # present, but not a record

    with pytest.raises(Exception):
        _lookup(tmp_path, "target.py", TARGET_HASH)
