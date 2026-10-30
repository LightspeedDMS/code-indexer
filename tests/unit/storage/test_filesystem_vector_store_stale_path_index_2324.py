"""#2324: a long-lived store (a server watch's) whose cached PathIndex is
stale reads a file's rows from chunks.db as they are now, so a change another
worker already applied converges; a missing established store still fails."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import MissingEstablishedChunkStoreError

VECTOR_SIZE = 16
COLLECTION = "coll"
PATH_FILTER = {"must": [{"key": "path", "match": {"value": "src/gamma.py"}}]}


def _point(point_id: str, path: str, seed: int) -> Dict:
    vector = np.random.default_rng(seed).standard_normal(VECTOR_SIZE)
    return {
        "id": point_id,
        "vector": vector.tolist(),
        "payload": {"path": path, "type": "content", "language": "python"},
    }


def _two_stores(base: Path) -> Tuple[FilesystemVectorStore, FilesystemVectorStore]:
    a = FilesystemVectorStore(base_path=base, use_chunks_db_for_new_collections=True)
    a.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    a.begin_indexing(COLLECTION)
    a.upsert_points(
        COLLECTION,
        [_point("keep-1", "src/keep.py", 1), _point("gamma-1", "src/gamma.py", 2)],
    )
    a.end_indexing(COLLECTION)
    assert _ids(a) == ["gamma-1"]
    b = FilesystemVectorStore(base_path=base, use_chunks_db_for_new_collections=True)
    return a, b


def _ids(store: FilesystemVectorStore) -> List[str]:
    page, _ = store.scroll_points(
        COLLECTION, limit=100, filter_conditions=PATH_FILTER, with_payload=True
    )
    return sorted(p["id"] for p in page)


def test_scroll_by_path_reads_rows_another_store_rewrote(tmp_path: Path) -> None:
    a, b = _two_stores(tmp_path)

    b.upsert_points(COLLECTION, [_point("gamma-2", "src/gamma.py", 3)])

    assert _ids(a) == ["gamma-2"]


def test_scroll_by_path_of_removed_rows_is_empty(tmp_path: Path) -> None:
    a, b = _two_stores(tmp_path)

    b.delete_points(COLLECTION, ["gamma-1"])

    assert _ids(a) == []


def test_scroll_by_path_still_fails_when_the_established_store_is_missing(
    tmp_path: Path,
) -> None:
    a, _ = _two_stores(tmp_path)

    (tmp_path / COLLECTION / "chunks.db").unlink()

    with pytest.raises(MissingEstablishedChunkStoreError):
        _ids(a)
