"""Bug #1529: temporal row-existence detection must be chunk-layout aware.

``temporal_shard_has_committed_rows`` scanned ONLY ``vector_*.json``. Bug
#1528 then made temporal indexing write the consolidated ``chunks.db``
layout, so a fully-populated temporal shard began reporting "no data".

That is a real cost bug, not a cosmetic one: ``golden_repo_manager``'s
``_temporal_vectors_exist_for_repo()`` uses this predicate to decide whether
an explicit temporal add-indexes/reindex must pass ``--clear``, so a false
negative forced a FULL re-embed of an entire git history (real embedding
spend) on every such run.

Real FilesystemVectorStore writes, real SQLite chunk stores, real files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

SHARD_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8


def _rows() -> List[Dict[str, Any]]:
    rng = np.random.default_rng(1529)
    return [
        {
            "id": "proj:commit:abcdef12:0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py", "commit_hash": "abcdef12"},
            "chunk_text": "def a(): ...",
        }
    ]


def _build(index_path: Path, *, chunks_db: bool, with_rows: bool) -> Path:
    store = FilesystemVectorStore(
        base_path=index_path, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    if with_rows:
        store.begin_indexing(SHARD_NAME)
        store.upsert_points(SHARD_NAME, _rows())
        store.end_indexing(SHARD_NAME)
    return index_path / SHARD_NAME


def test_consolidated_shard_with_rows_is_detected(tmp_path: Path) -> None:
    shard = _build(tmp_path / "index", chunks_db=True, with_rows=True)
    assert (shard / "chunks.db").is_file()
    assert list(shard.rglob("vector_*.json")) == []

    assert temporal_shard_has_committed_rows(shard) is True


def test_legacy_sharded_json_shard_with_rows_is_still_detected(
    tmp_path: Path,
) -> None:
    shard = _build(tmp_path / "index", chunks_db=False, with_rows=True)
    assert list(shard.rglob("vector_*.json"))

    assert temporal_shard_has_committed_rows(shard) is True


def test_empty_consolidated_collection_is_not_detected(tmp_path: Path) -> None:
    """The fix must not degrade into 'the directory/chunks.db exists'."""
    shard = _build(tmp_path / "index", chunks_db=True, with_rows=False)
    assert temporal_shard_has_committed_rows(shard) is False


def test_empty_legacy_collection_is_not_detected(tmp_path: Path) -> None:
    shard = _build(tmp_path / "index", chunks_db=False, with_rows=False)
    assert temporal_shard_has_committed_rows(shard) is False


def test_missing_directory_is_not_detected(tmp_path: Path) -> None:
    assert temporal_shard_has_committed_rows(tmp_path / "nope") is False


def test_inspection_never_creates_a_chunks_db(tmp_path: Path) -> None:
    """A CHUNKS_DB-flagged collection whose chunks.db is absent must answer
    False WITHOUT creating the file -- this is a read-only predicate."""
    shard = _build(tmp_path / "index", chunks_db=True, with_rows=True)
    (shard / "chunks.db").unlink()

    assert temporal_shard_has_committed_rows(shard) is False
    assert not (shard / "chunks.db").exists()
