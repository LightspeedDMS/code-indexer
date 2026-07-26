"""Story #1456: full fresh-build cycle for a CHUNKS_DB-mode collection
through the REAL public API (create_collection -> begin_indexing ->
upsert_points -> end_indexing -> search), proving the write path is not
inert -- this is the unit-level analog of "a real cidx index run producing
a real chunks.db-backed collection that queries correctly"."""

from unittest.mock import Mock

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

VECTOR_DIM = 24


def _points(vectors) -> list:
    return [
        {
            "id": f"vec_{i}",
            "vector": v.astype(np.float32).tolist(),
            "payload": {"path": f"vec_{i}.py", "language": "python"},
        }
        for i, v in enumerate(vectors)
    ]


class TestFreshChunksDbBuildEndToEnd:
    def test_discriminator_committed_after_first_end_indexing(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        store.begin_indexing("coll")
        rng = np.random.default_rng(11)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(10)]
        store.upsert_points("coll", _points(vectors))
        result = store.end_indexing("coll")

        assert result["vectors_indexed"] == 10
        # Regression guard: _save_path_index runs BEFORE the discriminator
        # is committed (AC1 ordering) -- it must gate on the combined
        # _is_chunks_db_collection authority, not the bare resolver (which
        # would still see SHARDED_JSON at that point in a genuine fresh
        # build) or it silently writes an empty id_index.bin.
        assert not (collection_path / "id_index.bin").exists()
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

    def test_search_returns_correct_results_after_fresh_build(self, tmp_path):
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        store.begin_indexing("coll")
        rng = np.random.default_rng(22)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(15)]
        store.upsert_points("coll", _points(vectors))
        store.end_indexing("coll")

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) > 0
        assert results[0]["id"] == "vec_0"
        assert results[0]["score"] > 0.99

    def test_second_indexing_session_incrementally_updates(self, tmp_path):
        """A second cidx index invocation on the SAME collection (adding
        more points) must correctly extend the HNSW index via the
        incremental path -- proving _apply_incremental_hnsw_batch_update is
        CHUNKS_DB-aware, not just the first-build full-rebuild path."""
        store = FilesystemVectorStore(
            base_path=tmp_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_DIM)

        rng = np.random.default_rng(33)
        first_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        store.begin_indexing("coll")
        store.upsert_points("coll", _points(first_vectors))
        store.end_indexing("coll")

        # SECOND session: add 5 more points (fresh FilesystemVectorStore
        # instance, mirroring a brand new `cidx index` subprocess).
        store2 = FilesystemVectorStore(base_path=tmp_path)
        second_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        second_points = [
            {
                "id": f"vec2_{i}",
                "vector": v.astype(np.float32).tolist(),
                "payload": {"path": f"vec2_{i}.py", "language": "python"},
            }
            for i, v in enumerate(second_vectors)
        ]
        store2.begin_indexing("coll")
        store2.upsert_points("coll", second_points)
        result = store2.end_indexing("coll")

        assert result["vectors_indexed"] == 10

        results = store2.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=second_vectors[0].tolist(),
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec2_0"
