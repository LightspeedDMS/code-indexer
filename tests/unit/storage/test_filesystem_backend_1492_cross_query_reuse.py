"""Post-manual-E2E-test regression test (Story #1492 follow-up).

Proves the FilesystemVectorStore/FilesystemBackend-level half of the AC1
cross-request singleton fix in isolation, independent of the live server's
query-dispatch plumbing: TWO SEPARATE FilesystemBackend.get_vector_store_client()
calls (exactly what search_service.py's _perform_semantic_search does once
per query -- a fresh FilesystemVectorStore per call) against the SAME
on-disk collection must share the process-wide CollectionMetaCache singleton,
so the SECOND query's search() call triggers ZERO additional real parses.

Real on-disk SHARDED_JSON collection (HNSWIndexManager-built), real
FilesystemBackend/FilesystemVectorStore, real CollectionMetaCache singleton
-- no mocking of the storage layer under test.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.shared.chunk_store_cache import reset_global_chunk_store_cache
from code_indexer.storage.shared.collection_meta_cache import (
    reset_global_collection_meta_cache,
)

VECTOR_DIM = 16


def _make_hnsw_cache() -> HNSWIndexCache:
    return HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=60.0))


def _build_sharded_json_collection(
    project_root: Path, collection_name: str, vectors: list
) -> Path:
    """Build a real SHARDED_JSON collection directly on disk, bypassing
    FilesystemVectorStore.create_collection() (which would need its OWN
    per-call id_index_cache) -- mirrors the existing meta-cache test's
    helper, adapted for a bare index_dir rather than a store instance."""
    index_dir = project_root / ".code-indexer" / "index"
    collection_path = index_dir / collection_name
    collection_path.mkdir(parents=True, exist_ok=True)
    (collection_path / "collection_meta.json").write_text(
        json.dumps({"vector_size": VECTOR_DIM, "name": collection_name})
    )

    for i, vector in enumerate(vectors):
        point_id = f"vec_{i:04d}"
        record = {
            "id": point_id,
            "vector": vector.astype(np.float32).tolist(),
            "payload": {"path": f"{point_id}.py"},
            "chunk_text": f"content for {point_id}",
        }
        shard_dir = collection_path / point_id[:2] / point_id[2:4]
        shard_dir.mkdir(parents=True, exist_ok=True)
        (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))

    HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine").rebuild_from_vectors(
        collection_path
    )

    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON
    return collection_path


@pytest.fixture
def rng():
    return np.random.default_rng(14920)


def _reload_total(cache) -> int:
    counters = cache.counters()
    return int(counters["immutable"]["reload"] + counters["mutable"]["reload"])


class TestCrossQueryFilesystemVectorStoreReuse:
    """Two SEPARATE FilesystemBackend.get_vector_store_client() calls in
    server mode share the global CollectionMetaCache singleton."""

    def setup_method(self) -> None:
        reset_global_collection_meta_cache()
        reset_global_chunk_store_cache()

    def teardown_method(self) -> None:
        reset_global_collection_meta_cache()
        reset_global_chunk_store_cache()

    def test_two_separate_store_instances_share_one_real_parse(
        self, tmp_path: Path, rng
    ) -> None:
        from code_indexer.storage.shared.collection_meta_cache import (
            get_global_collection_meta_cache,
        )

        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        collection_name = "coll"
        _build_sharded_json_collection(tmp_path, collection_name, vectors)

        hnsw_cache = _make_hnsw_cache()

        # Query 1: a fresh FilesystemBackend + fresh get_vector_store_client()
        # call, exactly mirroring search_service.py's per-request construction.
        backend1 = FilesystemBackend(project_root=tmp_path, hnsw_index_cache=hnsw_cache)
        store1 = backend1.get_vector_store_client()
        results1 = store1.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name=collection_name,
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert len(results1) > 0

        meta_cache = get_global_collection_meta_cache()
        after_query1 = _reload_total(meta_cache)
        assert after_query1 >= 1, (
            "First-ever query must trigger at least one real parse"
        )

        # Query 2: a SEPARATE FilesystemBackend + SEPARATE
        # get_vector_store_client() call (simulating a second, later HTTP
        # request) against the SAME unchanged collection.
        backend2 = FilesystemBackend(project_root=tmp_path, hnsw_index_cache=hnsw_cache)
        store2 = backend2.get_vector_store_client()
        results2 = store2.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name=collection_name,
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        assert len(results2) > 0

        after_query2 = _reload_total(meta_cache)
        assert after_query2 == after_query1, (
            "A second query (fresh FilesystemVectorStore instance, same "
            "unchanged collection) must trigger ZERO additional real "
            "parses when the global singleton is correctly shared -- "
            f"got {after_query1} reload(s) after query 1 and "
            f"{after_query2} after query 2"
        )

    def test_cli_mode_does_not_share_singleton_across_queries(
        self, tmp_path: Path, rng
    ) -> None:
        """Sanity control: WITHOUT hnsw_index_cache (CLI/solo), each fresh
        FilesystemBackend gets its OWN private cache -- a second query
        against the same collection DOES trigger another real parse. This
        proves the assertion above is measuring the actual singleton
        wiring, not some other unrelated no-op cache effect."""
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        collection_name = "coll"
        _build_sharded_json_collection(tmp_path, collection_name, vectors)

        backend1 = FilesystemBackend(project_root=tmp_path)
        store1 = backend1.get_vector_store_client()
        store1.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name=collection_name,
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        after_query1 = _reload_total(store1._collection_meta_cache)

        backend2 = FilesystemBackend(project_root=tmp_path)
        store2 = backend2.get_vector_store_client()
        store2.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name=collection_name,
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        after_query2 = _reload_total(store2._collection_meta_cache)

        # Each store has its OWN fresh cache (never shared) -- query 2's
        # cache starts cold and must reload independently of query 1's.
        assert after_query1 >= 1
        assert after_query2 >= 1
        assert store1._collection_meta_cache is not store2._collection_meta_cache
