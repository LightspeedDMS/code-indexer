"""Story #1456 AC3: _rebuild_path_index_from_disk() and scroll_points()'s
rglob safety-valve for CHUNKS_DB collections -- stream from chunks.db
instead of rglob-scanning vector_*.json files."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _record(point_id: str, path: str, **payload_extra) -> dict:
    vector = np.random.default_rng(6).standard_normal(VECTOR_DIM).astype(np.float32)
    payload = {"path": path}
    payload.update(payload_extra)
    return {"id": point_id, "vector": vector.tolist(), "payload": payload}


def _seed(tmp_path, records):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("coll")

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()
    write_chunks_db_discriminator(collection_path)
    return store


class TestRebuildPathIndexFromDiskChunksDb:
    def test_rebuilds_from_chunk_store_records(self, tmp_path):
        store = _seed(
            tmp_path,
            [_record("v0", "a.py"), _record("v1", "a.py"), _record("v2", "b.py")],
        )

        path_index = store._rebuild_path_index_from_disk("coll")

        assert path_index.get_point_ids("a.py") == {"v0", "v1"}
        assert path_index.get_point_ids("b.py") == {"v2"}


class TestScrollPointsRglobSafetyValveChunksDb:
    def test_scroll_without_path_filter_returns_points_from_chunk_store(self, tmp_path):
        store = _seed(
            tmp_path,
            [
                _record("v0", "a.py", language="python"),
                _record("v1", "b.py", language="rust"),
            ],
        )

        points, next_offset = store.scroll_points("coll", limit=10)

        ids = {p["id"] for p in points}
        assert ids == {"v0", "v1"}

    def test_scroll_applies_non_path_filter_conditions(self, tmp_path):
        store = _seed(
            tmp_path,
            [
                _record("v0", "a.py", language="python"),
                _record("v1", "b.py", language="rust"),
            ],
        )

        points, _ = store.scroll_points(
            "coll", limit=10, filter_conditions={"language": "python"}
        )

        assert [p["id"] for p in points] == ["v0"]
