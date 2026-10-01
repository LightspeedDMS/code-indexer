"""Story #1492 AC3: search() reuses the ChunkStore across repeat queries
against the same mutable CHUNKS_DB collection, instead of reopening
(with schema DDL) on every query.

Finding C5 (MEDIUM, report rank 11): open_chunk_store_for_path() was
called once per query. This test proves, via the REAL production
search() method against a real on-disk CHUNKS_DB collection:

- A repeat search() call from the SAME thread reuses the SAME ChunkStore
  object (object identity), never reopening the connection.
- Results are identical across repeat calls (no functional regression).
- A rebuild that replaces chunks.db (fresh HNSW+chunks.db pair, mirroring
  a re-index) with DIFFERENT point ids is correctly picked up on the next
  search() -- the cached handle is never used to serve the OLD dataset.

Real FilesystemVectorStore, real ChunkStore, real HNSW index, real
`precomputed_query_vector` (no embedding-provider network call needed) --
no mocking of the storage layer OR the embedding provider under test.
`_UnusedEmbeddingProvider` is a plain, never-invoked placeholder object
(precomputed_query_vector skips generate_embedding() entirely), not a
mock, matching this project's `--fts`-style "avoid mocking real
collaborators" standard even for an argument this call path never uses.
Spying on ChunkStoreThreadCache is done via subclassing (real
inheritance/injection), never via monkeypatching the class.
"""

import os
from pathlib import Path

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.shared.chunk_store_cache import ChunkStoreThreadCache
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 16


class _UnusedEmbeddingProvider:
    """Placeholder passed as `embedding_provider` -- never invoked because
    every search() call below supplies `precomputed_query_vector`, which
    skips `generate_embedding()`/`coalesced_query_embedding()` entirely."""


class _SpyingChunkStoreThreadCache(ChunkStoreThreadCache):
    """Real subclass (not a monkeypatch) recording every ChunkStore
    object returned by get_or_open(), for object-identity assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.opened_stores: list = []

    def get_or_open(self, db_path, collection_path, *, read_only=False):  # type: ignore[override]
        store_obj = super().get_or_open(db_path, collection_path, read_only=read_only)
        self.opened_stores.append(store_obj)
        return store_obj


def _build_chunks_db_collection(
    store: FilesystemVectorStore, collection_name: str, records: list
) -> Path:
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path(collection_name))

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)
    HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
        collection_path
    )
    return collection_path


def _record(point_id: str, vector: np.ndarray) -> dict:
    return {
        "id": point_id,
        "vector": vector.astype(np.float32).tolist(),
        "payload": {"path": f"{point_id}.py"},
        "chunk_text": f"content for {point_id}",
    }


def _force_future_mtime(path: Path) -> None:
    """Deterministically force a distinct, later mtime on `path` -- avoids
    relying on real-clock sleep + filesystem timestamp granularity."""
    future = os.stat(path).st_mtime + 60.0
    os.utime(path, (future, future))


@pytest.fixture
def rng():
    return np.random.default_rng(1492)


class TestSearchReusesChunkStoreAcrossCalls:
    def test_repeat_search_reuses_same_chunk_store_object(self, tmp_path, rng):
        chunk_store_cache = _SpyingChunkStoreThreadCache()
        store = FilesystemVectorStore(
            base_path=tmp_path, chunk_store_cache=chunk_store_cache
        )
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        records = [_record(f"vec_{i:04d}", v) for i, v in enumerate(vectors)]
        _build_chunks_db_collection(store, "coll", records)

        results1 = store.search(
            query="unused",
            embedding_provider=_UnusedEmbeddingProvider(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        results2 = store.search(
            query="unused",
            embedding_provider=_UnusedEmbeddingProvider(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results1) > 0
        assert [r["id"] for r in results1] == [r["id"] for r in results2]

        # AC3: the SAME ChunkStore object served both calls -- proves the
        # connection (and its already-executed schema DDL) was reused, not
        # reopened. ChunkStore.__init__ is the only call site of
        # _ensure_schema(), so object-identity reuse IS the DDL-not-
        # re-executed proof.
        assert len(chunk_store_cache.opened_stores) >= 2
        assert chunk_store_cache.opened_stores[0] is chunk_store_cache.opened_stores[1]

    def test_rebuilt_chunks_db_with_different_ids_is_observed_not_stale(
        self, tmp_path, rng
    ):
        chunk_store_cache = ChunkStoreThreadCache()
        store = FilesystemVectorStore(
            base_path=tmp_path, chunk_store_cache=chunk_store_cache
        )
        old_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(3)]
        old_records = [_record(f"old_{i:04d}", v) for i, v in enumerate(old_vectors)]
        _build_chunks_db_collection(store, "coll", old_records)

        results1 = store.search(
            query="unused",
            embedding_provider=_UnusedEmbeddingProvider(),
            collection_name="coll",
            limit=1,
            precomputed_query_vector=old_vectors[0].tolist(),
        )
        assert results1[0]["id"] == "old_0000"

        # Rebuild the collection with a DISJOINT id namespace at the SAME
        # path, forcing a real, deterministic mtime change on chunks.db.
        import shutil

        shutil.rmtree(tmp_path / "coll")
        new_vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(3)]
        new_records = [_record(f"new_{i:04d}", v) for i, v in enumerate(new_vectors)]
        collection_path = _build_chunks_db_collection(store, "coll", new_records)
        _force_future_mtime(collection_path / "chunks.db")

        results2 = store.search(
            query="unused",
            embedding_provider=_UnusedEmbeddingProvider(),
            collection_name="coll",
            limit=1,
            precomputed_query_vector=new_vectors[0].tolist(),
        )
        # Unambiguous non-staleness proof: the NEW id namespace is
        # returned, and the OLD id namespace never appears.
        assert results2[0]["id"] == "new_0000"
        assert results2[0]["id"] != "old_0000"
