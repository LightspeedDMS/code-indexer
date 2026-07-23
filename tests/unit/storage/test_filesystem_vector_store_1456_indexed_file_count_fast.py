"""Story #1456 AC3/AC7: get_indexed_file_count_fast()'s deep fallback
(no unique_file_count metadata AND no file_path_cache yet) for CHUNKS_DB
collections -- uses the EXACT chunks.db distinct-path count instead of a
vector-count estimate over the retired id_index.bin (which stays EMPTY for
CHUNKS_DB collections, so the unfixed legacy fallback always floors to 1
regardless of real data -- 2 distinct files here makes the real answer 2,
genuinely distinguishing fixed vs. unfixed behavior)."""

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


class TestGetIndexedFileCountFastChunksDbDeepFallback:
    def test_exact_count_via_chunk_store_distinct_paths(self, tmp_path):
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
                    "payload": {"path": "a.py" if i < 3 else "b.py"},
                }
                for i in range(5)  # 3 chunks in a.py, 2 chunks in b.py
            ]
            chunk_store.write_batch(records)
        finally:
            chunk_store.close()
        write_chunks_db_discriminator(collection_path)

        # No unique_file_count metadata yet, no file_path_cache populated --
        # the unfixed legacy fallback would return 1 (id_index stays empty
        # for CHUNKS_DB, so max(1, 0 // 2) == 1) regardless of real data.
        # The exact answer via chunks.db distinct_paths() is 2.
        assert store.get_indexed_file_count_fast("coll") == 2
