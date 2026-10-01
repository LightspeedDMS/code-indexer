"""search() query hot-path hydration for CHUNKS_DB collections (Story #1456
AC4, Epic #1454) + the AC7 critical threading requirement.

AC4:
- Unfiltered (Case A): HNSW candidates already come back in descending
  similarity order; the top `limit` candidates are taken FIRST, then
  hydrated from chunks.db -- at most `limit` chunk-store reads.
- Filtered/overfetch (Case B): payload filter evaluated per HNSW candidate,
  reading MORE than `limit` candidates is expected. The early-exit is
  CONDITIONAL on `lazy_load`; both flag values must return the SAME final
  rows (HNSW already returns candidates in score order, so early-exit on
  the first `limit` filter-passing candidates equals sorting the full
  filter-passing set and truncating to `limit`).

AC7 (critical, binding design decision): the HNSW-load worker thread must
perform ZERO id-index/chunk-store point-id resolution for CHUNKS_DB
collections -- point-id resolution happens exclusively via the chunk store,
opened only after the HNSW-load worker's .result() returns to the main
thread. Proven here via real spies on the EXTERNAL collaborators
(`IDIndexManager.load_index`, `pathlib.Path.rglob`) -- never by patching
FilesystemVectorStore's own methods.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 32


def _build_chunks_db_collection(
    store: FilesystemVectorStore,
    collection_name: str,
    records: list,
    vector_dim: int = VECTOR_DIM,
) -> Path:
    """Create a collection, populate chunks.db, build its HNSW index from
    chunks.db (discriminator committed BEFORE rebuild so rebuild resolves
    CHUNKS_DB -- mirrors a re-index of an already-consolidated collection),
    then leave the discriminator set as the collection's steady state."""
    store.create_collection(collection_name, vector_size=vector_dim)
    collection_path = Path(store._get_collection_path(collection_name))

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)

    hnsw_manager = HNSWIndexManager(vector_dim=vector_dim, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


def _record(point_id: str, vector: np.ndarray, **payload_extra) -> dict:
    payload = {"path": f"{point_id}.py", "type": "content"}
    payload.update(payload_extra)
    return {
        "id": point_id,
        "vector": vector.astype(np.float32).tolist(),
        "payload": payload,
        "chunk_text": f"chunk text for {point_id}",
    }


@pytest.fixture
def rng():
    return np.random.default_rng(1234)


class TestSearchChunksDbUnfilteredCaseA:
    def test_top_result_is_exact_query_vector_match(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(10)]
        records = [_record(f"vec_{i}", vectors[i]) for i in range(10)]
        _build_chunks_db_collection(store, "coll", records)

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) <= 5
        assert len(results) > 0
        assert results[0]["id"] == "vec_0"
        assert results[0]["score"] > 0.99
        assert all("payload" in r for r in results)
        assert all("score" in r for r in results)
        # payload/content correctly hydrated from chunks.db
        assert results[0]["payload"]["path"] == "vec_0.py"

    def test_scores_descending_and_limit_respected(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(20)]
        records = [_record(f"vec_{i}", vectors[i]) for i in range(20)]
        _build_chunks_db_collection(store, "coll", records)

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=7,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) <= 7
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_unfiltered_hydration_performs_at_most_limit_chunk_store_reads(
        self, tmp_path, rng
    ) -> None:
        """AC4: unfiltered (Case A) hydration takes the top `limit`
        candidates FIRST, then hydrates ONLY those -- at most `limit`
        ChunkStore.read() calls, even though the HNSW candidate set (hnsw_k
        = limit * 2 by default) is larger. Real autospec spy: the original
        ChunkStore.read implementation still executes via side_effect."""
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(40)]
        records = [_record(f"vec_{i}", vectors[i]) for i in range(40)]
        _build_chunks_db_collection(store, "coll", records)

        limit = 5
        with patch.object(
            ChunkStore, "read", autospec=True, side_effect=ChunkStore.read
        ) as read_spy:
            results = store.search(
                query="unused",
                embedding_provider=Mock(),
                collection_name="coll",
                limit=limit,
                precomputed_query_vector=vectors[0].tolist(),
            )

        assert len(results) > 0
        assert read_spy.call_count <= limit


class TestSearchChunksDbFilteredCaseBOverfetch:
    def test_filter_conditions_only_return_matching_payloads(self, tmp_path, rng):
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(20)]
        records = []
        for i in range(20):
            lang = "python" if i % 2 == 0 else "javascript"
            records.append(_record(f"vec_{i}", vectors[i], language=lang))
        _build_chunks_db_collection(store, "coll", records)

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            filter_conditions={"language": "python"},
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) > 0
        assert all(r["payload"]["language"] == "python" for r in results)

    def test_lazy_load_true_and_false_return_identical_rows(self, tmp_path, rng):
        """AC4: filtered/overfetch hydration returns byte-identical full
        result objects for both lazy_load flag values (these are non-git
        tmp_path collections, so staleness detection is deterministic --
        `is_stale: False`, no timestamps -- making full-dict equality safe)."""
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(30)]
        records = []
        for i in range(30):
            lang = "python" if i % 3 == 0 else "javascript"
            records.append(_record(f"vec_{i}", vectors[i], language=lang))
        _build_chunks_db_collection(store, "coll", records)

        common_kwargs = dict(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=4,
            filter_conditions={"language": "python"},
            precomputed_query_vector=vectors[0].tolist(),
            prefetch_limit=25,
        )

        results_lazy = store.search(lazy_load=True, **common_kwargs)
        results_eager = store.search(lazy_load=False, **common_kwargs)

        assert results_lazy == results_eager
        assert len(results_lazy) > 0


class TestSearchChunksDbWorkerThreadIsolationAC7:
    def test_id_index_manager_and_rglob_never_invoked_for_chunks_db_collection(
        self, tmp_path, rng
    ) -> None:
        """AC7 critical binding requirement: NO id-index/chunk-store path
        resolution happens anywhere in search() for a CHUNKS_DB collection --
        point-id resolution happens exclusively via the chunk store. Proven
        via real spies on two EXTERNAL collaborators (never FilesystemVectorStore
        itself): IDIndexManager.load_index (the id_index.bin binary loader)
        and pathlib.Path.rglob (the vector_*.json fallback scanner) -- both
        are the only two code paths `_load_id_index()` could possibly take,
        so proving neither fires proves the id-index machinery is never
        touched at all for this collection.
        """
        store = FilesystemVectorStore(base_path=tmp_path)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(10)]
        records = [_record(f"vec_{i}", vectors[i]) for i in range(10)]
        _build_chunks_db_collection(store, "coll", records)

        with (
            patch.object(
                IDIndexManager,
                "load_index",
                autospec=True,
                side_effect=IDIndexManager.load_index,
            ) as id_index_spy,
            patch.object(
                Path, "rglob", autospec=True, side_effect=Path.rglob
            ) as rglob_spy,
        ):
            results = store.search(
                query="unused",
                embedding_provider=Mock(),
                collection_name="coll",
                limit=5,
                precomputed_query_vector=vectors[0].tolist(),
            )

        # HNSW load must still succeed via hnsw_index.id_mapping alone.
        assert len(results) > 0
        assert results[0]["id"] == "vec_0"
        assert id_index_spy.call_count == 0
        vector_json_rglob_calls = [
            call
            for call in rglob_spy.call_args_list
            if call.args[1:] == ("vector_*.json",)
        ]
        assert vector_json_rglob_calls == []
