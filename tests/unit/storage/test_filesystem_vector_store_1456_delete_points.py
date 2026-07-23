"""Story #1456 AC7: id_index.bin retirement for delete_points() on CHUNKS_DB
collections -- must delete via ChunkStore.delete(), never create OR consult
id_index.bin.
"""

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.id_index_manager import IDIndexManager
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


class TestDeletePointsChunksDb:
    def test_delete_removes_point_from_chunk_store(self, store):
        records = [_record("v0"), _record("v1"), _record("v2")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        result = store.delete_points("coll", ["v1"])

        assert result["status"] == "ok"
        assert result["deleted"] == 1

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            assert chunk_store.read("v1") is None
            assert chunk_store.read("v0") is not None
            assert chunk_store.read("v2") is not None
        finally:
            chunk_store.close()

    def test_delete_never_creates_id_index_bin(self, store):
        records = [_record("v0"), _record("v1")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        store.delete_points("coll", ["v0"])

        assert not (collection_path / "id_index.bin").exists()

    def test_delete_ignores_a_stale_adversarial_id_index_bin(self, store, tmp_path):
        """Plant a deliberately WRONG id_index.bin (mapping v0 to a path
        that does not exist) BEFORE calling delete_points(). If delete_points
        consulted id_index.bin at all, it would fail to find/delete v0 (since
        the planted mapping points nowhere real). Proving the real chunk-store
        deletion succeeds anyway proves id_index.bin is never read."""
        records = [_record("v0"), _record("v1")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        bogus_path = tmp_path / "adversarial" / "missing.json"
        IDIndexManager().save_index(collection_path, {"v0": bogus_path})
        assert (collection_path / "id_index.bin").exists()
        assert not bogus_path.exists()

        result = store.delete_points("coll", ["v0"])

        assert result["status"] == "ok"
        assert result["deleted"] == 1

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            assert chunk_store.read("v0") is None
            assert chunk_store.read("v1") is not None
        finally:
            chunk_store.close()

    def test_delete_nonexistent_point_id_is_noop(self, store):
        records = [_record("v0")]
        _build_chunks_db_collection(store, "coll", records)

        result = store.delete_points("coll", ["does-not-exist"])

        assert result["status"] == "ok"
        assert result["deleted"] == 0
