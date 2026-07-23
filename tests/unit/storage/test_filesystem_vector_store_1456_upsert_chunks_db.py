"""Story #1456: upsert_points() production write path for CHUNKS_DB-mode
collections -- writes go into chunks.db (via ChunkStore.write_batch), never
into per-point vector_*.json files."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _points(n: int, prefix: str = "vec") -> list:
    rng = np.random.default_rng(3)
    return [
        {
            "id": f"{prefix}_{i}",
            "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
            "payload": {"path": f"{prefix}_{i}.py", "language": "python"},
        }
        for i in range(n)
    ]


class TestUpsertPointsWritesToChunksDb:
    def test_upsert_writes_records_readable_via_chunk_store(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        store.begin_indexing("coll")
        store.upsert_points("coll", _points(5))
        store.end_indexing("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            assert chunk_store.count() == 5
            record = chunk_store.read("vec_0")
        finally:
            chunk_store.close()

        assert record is not None
        assert record["payload"]["path"] == "vec_0.py"

    def test_upsert_never_creates_vector_json_files(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        store.begin_indexing("coll")
        store.upsert_points("coll", _points(5))
        store.end_indexing("coll")

        assert list(collection_path.rglob("vector_*.json")) == []

    def test_upsert_tracks_session_changes_for_incremental_hnsw(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(3))

        changes = store._indexing_session_changes["coll"]
        assert changes["added"] == {"vec_0", "vec_1", "vec_2"}
