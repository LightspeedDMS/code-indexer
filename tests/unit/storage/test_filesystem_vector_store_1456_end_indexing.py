"""Story #1456 AC7: id_index.bin retirement for end_indexing() and
_save_path_index() on CHUNKS_DB collections -- the indexing finalize path
must never create id_index.bin, and vector_count must be reported correctly
via the chunk store (not the empty in-memory id_index dict).
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


def _record(point_id: str) -> dict:
    return {
        "id": point_id,
        "vector": np.random.default_rng(0)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": {"path": f"{point_id}.py"},
        "chunk_text": f"content {point_id}",
    }


@pytest.fixture
def store(tmp_path):
    return FilesystemVectorStore(base_path=tmp_path)


class TestEndIndexingChunksDb:
    def test_end_indexing_never_creates_id_index_bin(self, store):
        records = [_record("v0"), _record("v1"), _record("v2")]
        collection_path = _build_chunks_db_collection(store, "coll", records)

        store.end_indexing("coll")

        assert not (collection_path / "id_index.bin").exists()

    def test_end_indexing_reports_correct_vector_count(self, store):
        records = [_record("v0"), _record("v1"), _record("v2")]
        _build_chunks_db_collection(store, "coll", records)

        result = store.end_indexing("coll")

        assert result["vectors_indexed"] == 3
