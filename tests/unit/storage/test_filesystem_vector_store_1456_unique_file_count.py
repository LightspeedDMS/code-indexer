"""Story #1456 AC3/AC7: _calculate_and_save_unique_file_count() for
CHUNKS_DB collections -- derives unique file paths from chunks.db
(ChunkStore.distinct_paths) instead of opening per-point vector JSON files
via id_index.bin."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


def _record(point_id: str, path: str) -> dict:
    return {
        "id": point_id,
        "vector": np.random.default_rng(1)
        .standard_normal(VECTOR_DIM)
        .astype(np.float32)
        .tolist(),
        "payload": {"path": path},
        "chunk_text": "x",
    }


class TestCalculateAndSaveUniqueFileCountChunksDb:
    def test_counts_unique_files_across_multi_chunk_files(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch(
                [
                    _record("v0", "a.py"),
                    _record("v1", "a.py"),  # same file, 2nd chunk
                    _record("v2", "b.py"),
                ]
            )
        finally:
            chunk_store.close()
        write_chunks_db_discriminator(collection_path)
        HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
            collection_path
        )

        count = store._calculate_and_save_unique_file_count("coll", collection_path)

        assert count == 2

    def test_writes_result_into_collection_metadata(self, tmp_path):
        import json

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch([_record("v0", "a.py"), _record("v1", "b.py")])
        finally:
            chunk_store.close()
        write_chunks_db_discriminator(collection_path)

        store._calculate_and_save_unique_file_count("coll", collection_path)

        meta = json.loads((collection_path / "collection_meta.json").read_text())
        assert meta["unique_file_count"] == 2
