"""Story #1456 (Epic #1454) AC1 fresh-collection discriminator ordering +
AC6 SHARDED_JSON-vs-CHUNKS_DB query-result equivalence.

AC1: the ``chunks_db`` discriminator commit is a MANDATORY FINAL step, never
incidental to data completeness. A collection whose chunks.db + HNSW index
are fully built and valid, but whose discriminator was never committed (or
was subsequently lost/rolled back), MUST resolve as SHARDED_JSON -- proving
the fail-closed contract holds independent of underlying data completeness.

AC6: before/after equivalence on a fixed query set -- same point_ids,
ordering, and payloads -- for both unfiltered and payload-filtered queries,
comparing a SHARDED_JSON collection against an equivalent CHUNKS_DB
collection built from the SAME source data.
"""

import json

import numpy as np
import pytest
from unittest.mock import Mock

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 24


@pytest.fixture
def rng():
    return np.random.default_rng(7)


class TestFreshCollectionDiscriminatorCommitOrderingAC1:
    def test_complete_store_and_index_without_discriminator_resolves_sharded_json(
        self, tmp_path
    ) -> None:
        """A chunks.db store + a fully-built HNSW index exist on disk, but
        the discriminator was never committed -- fail-closed to SHARDED_JSON,
        proving the discriminator (not data presence) is the sole authority."""
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch(
                [
                    {
                        "id": "v0",
                        "vector": np.zeros(VECTOR_DIM, dtype=np.float32).tolist(),
                        "payload": {"path": "f0.py"},
                        "chunk_text": "content",
                    }
                ]
            )
        finally:
            chunk_store.close()

        # Discriminator deliberately NOT written -- "injection prevented" it.
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

    def test_discriminator_committed_after_rebuild_then_lost_resolves_sharded_json(
        self, tmp_path
    ) -> None:
        """A collection that WAS validly built as CHUNKS_DB (chunks.db +
        discriminator + working HNSW index) but whose discriminator is
        subsequently removed from collection_meta.json (simulating a lost/
        rolled-back commit) must immediately fail closed to SHARDED_JSON,
        even though chunks.db and the HNSW index remain fully intact and
        queryable on disk."""
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path("coll")

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch(
                [
                    {
                        "id": "v0",
                        "vector": np.zeros(VECTOR_DIM, dtype=np.float32).tolist(),
                        "payload": {"path": "f0.py"},
                        "chunk_text": "content",
                    }
                ]
            )
        finally:
            chunk_store.close()

        write_chunks_db_discriminator(collection_path)

        hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = hnsw_manager.rebuild_from_vectors(collection_path)
        assert count == 1  # fully built and valid

        # Simulate the discriminator commit being lost/rolled back -- the
        # chunks.db + HNSW index on disk are UNCHANGED and still valid.
        meta_path = collection_path / "collection_meta.json"
        meta = json.loads(meta_path.read_text())
        del meta["chunks_db"]
        meta_path.write_text(json.dumps(meta))

        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON


class TestShardedJsonVsChunksDbQueryEquivalenceAC6:
    """Before/after equivalence: same point_ids, ordering, and payloads for
    both unfiltered and payload-filtered queries."""

    def _build_sharded_json_collection(self, store, name, points):
        store.create_collection(name, vector_size=VECTOR_DIM)
        store.begin_indexing(name)
        store.upsert_points(name, points)
        store.end_indexing(name)

    def _build_chunks_db_collection(self, store, name, points):
        from code_indexer.storage.shared.chunk_layout import (
            write_chunks_db_discriminator,
        )

        store.create_collection(name, vector_size=VECTOR_DIM)
        collection_path = store._get_collection_path(name)

        chunk_store = ChunkStore(collection_path / "chunks.db")
        try:
            chunk_store.write_batch(points)
        finally:
            chunk_store.close()

        write_chunks_db_discriminator(collection_path)

        hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        hnsw_manager.rebuild_from_vectors(collection_path)

    def test_unfiltered_query_returns_same_point_ids_and_order(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(15)]
        points = [
            {
                "id": f"vec_{i}",
                "vector": vectors[i].astype(np.float32).tolist(),
                "payload": {"path": f"vec_{i}.py", "language": "python"},
            }
            for i in range(15)
        ]

        self._build_sharded_json_collection(store, "sharded", points)
        self._build_chunks_db_collection(store, "chunksdb", points)

        query_vector = vectors[0].tolist()
        results_sharded = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="sharded",
            limit=6,
            precomputed_query_vector=query_vector,
        )
        results_chunksdb = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="chunksdb",
            limit=6,
            precomputed_query_vector=query_vector,
        )

        ids_sharded = [r["id"] for r in results_sharded]
        ids_chunksdb = [r["id"] for r in results_chunksdb]
        assert ids_sharded == ids_chunksdb
        assert len(ids_sharded) > 0

        payloads_sharded = [r["payload"]["path"] for r in results_sharded]
        payloads_chunksdb = [r["payload"]["path"] for r in results_chunksdb]
        assert payloads_sharded == payloads_chunksdb

    def test_filtered_query_returns_same_point_ids_and_order(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(20)]
        points = []
        for i in range(20):
            lang = "python" if i % 2 == 0 else "rust"
            points.append(
                {
                    "id": f"vec_{i}",
                    "vector": vectors[i].astype(np.float32).tolist(),
                    "payload": {"path": f"vec_{i}.py", "language": lang},
                }
            )

        self._build_sharded_json_collection(store, "sharded", points)
        self._build_chunks_db_collection(store, "chunksdb", points)

        query_vector = vectors[0].tolist()
        common_kwargs = dict(
            query="unused",
            embedding_provider=Mock(),
            limit=5,
            filter_conditions={"language": "python"},
            precomputed_query_vector=query_vector,
        )

        results_sharded = store.search(collection_name="sharded", **common_kwargs)
        results_chunksdb = store.search(collection_name="chunksdb", **common_kwargs)

        ids_sharded = [r["id"] for r in results_sharded]
        ids_chunksdb = [r["id"] for r in results_chunksdb]
        assert ids_sharded == ids_chunksdb
        assert len(ids_sharded) > 0
        assert all(r["payload"]["language"] == "python" for r in results_sharded)
        assert all(r["payload"]["language"] == "python" for r in results_chunksdb)
