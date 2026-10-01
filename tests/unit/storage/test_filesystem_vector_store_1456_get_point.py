"""Story #1456 AC7: id_index.bin retirement for get_point() on CHUNKS_DB
collections -- must resolve via the chunk store, never id_index.bin.
"""

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _build_chunks_db_collection(
    store: FilesystemVectorStore, collection_name: str, records: list
):
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path(collection_name)

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


def _record(point_id: str, **payload_extra) -> dict:
    payload = {"path": f"{point_id}.py"}
    payload.update(payload_extra)
    return {
        "id": point_id,
        "vector": np.random.default_rng(0)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": payload,
        "chunk_text": f"content {point_id}",
    }


@pytest.fixture
def store(tmp_path):
    return FilesystemVectorStore(base_path=tmp_path)


class TestGetPointChunksDb:
    def test_get_point_resolves_via_chunk_store(self, store):
        records = [_record("v0"), _record("v1")]
        _build_chunks_db_collection(store, "coll", records)

        result = store.get_point("v0", "coll")

        assert result is not None
        assert result["id"] == "v0"
        assert result["payload"]["path"] == "v0.py"
        assert result["chunk_text"] == "content v0"

    def test_get_point_returns_none_for_missing_id(self, store):
        records = [_record("v0")]
        _build_chunks_db_collection(store, "coll", records)

        assert store.get_point("does-not-exist", "coll") is None

    def test_get_point_never_creates_id_index_bin(self, store):
        records = [_record("v0")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        store.get_point("v0", "coll")

        assert not (collection_path / "id_index.bin").exists()
