"""Story #1492 AC1: FilesystemVectorStore.search() consolidates the 4-5
redundant collection_meta.json parses into at most ONE real parse per
search call per collection+mtime.

Report Finding C1 (SEVERE, rank 2, "highest ROI fix in the audit"): a
single search() call used to read+json.loads collection_meta.json 4-5
separate times (collection_exists(), the vector_size read, is_stale(),
and up to two resolve_chunk_layout() calls). This test measures the ACTUAL
parse count (via the real CollectionMetaCache's own TTLCache reload
counters -- Story #1082's public API, not a mock) BEFORE proving the fix:
one real search() call against a real on-disk SHARDED_JSON collection
must trigger at most one real parse, a second identical search() call
must trigger ZERO additional parses, and N distinct collections must
trigger N parses (never redundant re-parses of the same one).

Real FilesystemVectorStore, real HNSWIndexManager-built index, real
CollectionMetaCache -- no mocking of the storage layer under test.
embedding_provider=Mock() is only used because precomputed_query_vector
bypasses it entirely (established convention in this test suite, e.g.
test_filesystem_vector_store_1458_activation_cache_key.py).
"""

import json
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from code_indexer.server.cache.id_index_cache import IdIndexCache
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.shared.collection_meta_cache import CollectionMetaCache

VECTOR_DIM = 16


def _make_store(
    tmp_path: Path, meta_cache: CollectionMetaCache
) -> FilesystemVectorStore:
    # Explicit use_chunks_db_for_new_collections=False -- these tests
    # deliberately exercise the legacy SHARDED_JSON layout (proven below by
    # asserting resolve_chunk_layout() on the built collection), never
    # relying on the production default alone.
    #
    # id_index_cache=IdIndexCache() -- WITHOUT this, create_collection()'s
    # in-memory `self._id_index[cache_key] = {}` placeholder is never
    # refreshed by _load_id_index() (that only happens on the id_index_cache
    # code path), so search() would silently read an empty id_index and
    # find zero candidates -- a test-construction pitfall unrelated to this
    # story's caching logic, avoided here the same way the pre-existing
    # test_filesystem_vector_store_1458_activation_cache_key.py does.
    return FilesystemVectorStore(
        base_path=tmp_path,
        collection_meta_cache=meta_cache,
        id_index_cache=IdIndexCache(),
        use_chunks_db_for_new_collections=False,
    )


def _build_sharded_json_collection(
    store: FilesystemVectorStore, collection_name: str, vectors: list
) -> Path:
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path(collection_name))

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

    # Test-setup invariant: this collection must genuinely be SHARDED_JSON
    # (never CHUNKS_DB), or the parse-count assertions below would not be
    # exercising the scenario the intent declares.
    assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

    return collection_path


@pytest.fixture
def rng():
    return np.random.default_rng(1492)


def _reload_total(cache: CollectionMetaCache) -> int:
    counters = cache.counters()
    return int(counters["immutable"]["reload"] + counters["mutable"]["reload"])


class TestSearchParsesMetadataAtMostOncePerCall:
    def test_single_search_call_parses_at_most_once(self, tmp_path, rng):
        meta_cache = CollectionMetaCache()
        store = _make_store(tmp_path, meta_cache)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        _build_sharded_json_collection(store, "coll", vectors)

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) > 0
        # AC1: ONE search call -> AT MOST one real parse for this
        # collection+mtime (down from 4-5 in the pre-fix implementation).
        assert _reload_total(meta_cache) == 1

    def test_repeat_search_same_mtime_causes_zero_additional_parses(
        self, tmp_path, rng
    ):
        meta_cache = CollectionMetaCache()
        store = _make_store(tmp_path, meta_cache)
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]
        _build_sharded_json_collection(store, "coll", vectors)

        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        after_first = _reload_total(meta_cache)
        assert after_first == 1

        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=3,
            precomputed_query_vector=vectors[0].tolist(),
        )
        after_second = _reload_total(meta_cache)

        # A second search against the UNCHANGED collection must not
        # trigger ANY additional real parse.
        assert after_second == after_first


class TestMultiCollectionParseScaling:
    def test_parses_scale_with_distinct_collections_not_redundant_reparses(
        self, tmp_path, rng
    ):
        """Mirrors the multi-shard temporal requirement: N distinct
        collections searched once each -> N parses, never redundant
        re-parses of any single one."""
        meta_cache = CollectionMetaCache()
        store = _make_store(tmp_path, meta_cache)
        vectors_a = [rng.standard_normal(VECTOR_DIM) for _ in range(3)]
        vectors_b = [rng.standard_normal(VECTOR_DIM) for _ in range(3)]
        _build_sharded_json_collection(store, "shard_a", vectors_a)
        _build_sharded_json_collection(store, "shard_b", vectors_b)

        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="shard_a",
            limit=3,
            precomputed_query_vector=vectors_a[0].tolist(),
        )
        store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="shard_b",
            limit=3,
            precomputed_query_vector=vectors_b[0].tolist(),
        )

        assert _reload_total(meta_cache) == 2  # one per distinct collection
