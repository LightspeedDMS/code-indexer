"""Issue #1459 AC3: writer/reader collection_meta.json shared-contract test.

Readers that only read collection_meta.json's scalar fields (`vector_count`
under the top-level `hnsw_index` key, and the top-level `unique_file_count`)
are safe PROVIDED those fields keep being written accurately by the writer
path (Stories 1/2/3, already merged). This test drives the REAL public write
API end-to-end for a CHUNKS_DB-layout collection (no mocking of the storage
layer, per Messi Rule #1) and cross-verifies both the metadata fields AND
the reader APIs built on top of them (`count_points`,
`get_indexed_file_count_fast`) against ground truth obtained independently
via a freshly-opened `ChunkStore`.
"""

import json

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 24
_TOTAL_CHUNKS = 12
_DISTINCT_FILE_COUNT = 4


def _points_across_files(rng: np.random.Generator) -> list:
    """12 chunk points spread unevenly across 4 distinct file paths, so a
    per-file estimate (rather than an exact distinct-path count) would give
    the wrong unique_file_count if the writer regressed to the legacy
    estimation fallback."""
    file_paths = [
        "src/module_a.py",
        "src/module_b.py",
        "src/module_c.py",
        "src/module_d.py",
    ]
    # Uneven chunk-per-file distribution: 5, 3, 3, 1 chunks (sums to 12).
    chunks_per_file = [5, 3, 3, 1]
    points = []
    idx = 0
    for file_path, chunk_count in zip(file_paths, chunks_per_file):
        for _ in range(chunk_count):
            vector = rng.standard_normal(VECTOR_DIM).astype(np.float32)
            points.append(
                {
                    "id": f"vec_{idx}",
                    "vector": vector.tolist(),
                    "payload": {"path": file_path, "language": "python"},
                }
            )
            idx += 1
    return points


class TestChunksDbMetaContract:
    def test_writer_produces_accurate_vector_count_and_unique_file_count(
        self, tmp_path
    ):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        rng = np.random.default_rng(42)
        points = _points_across_files(rng)

        store.begin_indexing("coll")
        store.upsert_points("coll", points)
        result = store.end_indexing("coll")

        # 1. end_indexing()'s own return value.
        assert result["vectors_indexed"] == _TOTAL_CHUNKS
        assert result["unique_files"] == _DISTINCT_FILE_COUNT

        # 2. Ground truth via an INDEPENDENTLY opened ChunkStore (not the
        # store instance under test's own cached state).
        with ChunkStore(collection_path / "chunks.db") as verify_store:
            assert verify_store.count() == _TOTAL_CHUNKS
            assert len(verify_store.distinct_paths()) == _DISTINCT_FILE_COUNT

        # 3. collection_meta.json scalar-field contract: vector_count lives
        # under the top-level hnsw_index key; unique_file_count is top-level.
        meta_path = collection_path / "collection_meta.json"
        metadata = json.loads(meta_path.read_text())
        assert metadata["hnsw_index"]["vector_count"] == _TOTAL_CHUNKS
        assert metadata["unique_file_count"] == _DISTINCT_FILE_COUNT

        # 4. Reader APIs built on top of those fields must agree.
        assert store.count_points("coll") == _TOTAL_CHUNKS
        assert store.get_indexed_file_count_fast("coll") == _DISTINCT_FILE_COUNT

    def test_reader_apis_agree_from_a_fresh_store_instance(self, tmp_path):
        """A brand-new FilesystemVectorStore instance (no in-memory cache
        from the write session -- mirrors a fresh `cidx status`/`cidx
        query` process reading an already-indexed collection) must read the
        SAME accurate values purely from collection_meta.json / chunks.db."""
        writer = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        writer.create_collection("coll", vector_size=VECTOR_DIM)

        rng = np.random.default_rng(7)
        points = _points_across_files(rng)
        writer.begin_indexing("coll")
        writer.upsert_points("coll", points)
        writer.end_indexing("coll")

        reader = FilesystemVectorStore(base_path=tmp_path)
        assert reader.count_points("coll") == _TOTAL_CHUNKS
        assert reader.get_indexed_file_count_fast("coll") == _DISTINCT_FILE_COUNT
