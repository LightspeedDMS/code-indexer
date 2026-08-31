"""Story #1456 AC3/AC7: count_points()'s fallback path (no hnsw_index
metadata written yet) for CHUNKS_DB collections -- derives the count from
ChunkStore.count() instead of the retired id_index.bin."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


class TestCountPointsFallbackChunksDb:
    def test_counts_via_chunk_store_before_any_hnsw_metadata_exists(self, tmp_path):
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            records = [
                {
                    "id": f"v{i}",
                    "vector": np.random.default_rng(0)
                    .standard_normal(VECTOR_DIM)
                    .astype(np.float32)
                    .tolist(),
                    "payload": {"path": f"f{i}.py"},
                }
                for i in range(4)
            ]
            chunk_store.write_batch(records)
        finally:
            chunk_store.close()
        write_chunks_db_discriminator(collection_path)

        # NOTE: no hnsw_index key in collection_meta.json yet -- fast path
        # unavailable, this exercises the fallback exclusively.
        assert store.count_points("coll") == 4
