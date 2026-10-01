"""Bug #1736 Finding 1: warm-time cache key must equal query-time cache key.

``FilesystemVectorStore.hnsw_cache_key_for_collection()`` is the key the
daemon composes when it WARMS a collection into the shared ``HNSWIndexCache``
ahead of time. ``search()`` independently composes its own cache key at
QUERY time via ``self._activation_scoped_cache_key(str(collection_path
.resolve()), chunk_layout_token=_search_chunk_layout.value)``.

Both are documented (and were verified live during the #1730 remediation
review) to be byte-identical, but no prior test asserted this directly --
``test_semantic_search_uses_cache_for_matching_collection``
(tests/unit/services/daemon/test_daemon_cache_usage.py) uses a synthetic
literal key on BOTH the warm and query sides instead of deriving the
query-time key from the real production ``search()`` code path. A future
refactor of either key-composition function could silently diverge the two,
degrading daemon cache-hit performance to a permanent miss without any test
catching it (the failure mode is safe -- a miss, never a wrong-collection
hit -- but silent).

This test spies on ``HNSWIndexCache.get_or_load`` to capture the EXACT key
string a real, unmocked ``search()`` call passes for a real on-disk
SHARDED_JSON collection, then independently calls
``hnsw_cache_key_for_collection()`` for the SAME collection path + FSV
instance (same ``activation_id``), and asserts the two strings are equal.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.server.cache.hnsw_index_cache import HNSWIndexCache
from code_indexer.server.cache.id_index_cache import IdIndexCache
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

VECTOR_DIM = 16


def _build_sharded_json_collection(
    store: FilesystemVectorStore,
    collection_name: str,
    vectors: list,
) -> Path:
    """Build a real SHARDED_JSON-layout collection (legacy vector_*.json +
    a real HNSW index built from them) on disk, mirroring the established
    helper in test_filesystem_vector_store_1458_activation_cache_key.py."""
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

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


@pytest.fixture
def rng():
    return np.random.default_rng(1736)


class TestWarmTimeKeyEqualsQueryTimeKey:
    """Direct equivalence proof between the warm-time key composer
    (``hnsw_cache_key_for_collection()``) and the real key ``search()``
    composes and hands to ``HNSWIndexCache.get_or_load()``."""

    def test_hnsw_cache_key_for_collection_matches_the_key_search_actually_uses(
        self, tmp_path: Path, rng
    ) -> None:
        shared_hnsw_cache = HNSWIndexCache()
        shared_id_cache = IdIndexCache()
        vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(5)]

        store = FilesystemVectorStore(
            base_path=tmp_path,
            hnsw_index_cache=shared_hnsw_cache,
            id_index_cache=shared_id_cache,
            activation_id="warm-query-equivalence-activation-id",
        )
        collection_path = _build_sharded_json_collection(store, "coll", vectors)

        captured_query_time_keys: list = []
        original_get_or_load = HNSWIndexCache.get_or_load

        def _spy_get_or_load(self, repo_path, loader, index_file=None):
            captured_query_time_keys.append(repo_path)
            return original_get_or_load(self, repo_path, loader, index_file=index_file)

        with patch.object(HNSWIndexCache, "get_or_load", _spy_get_or_load):
            store.search(
                query="unused",
                embedding_provider=Mock(),
                collection_name="coll",
                limit=5,
                precomputed_query_vector=vectors[0].tolist(),
            )

        assert len(captured_query_time_keys) == 1, (
            "test setup invariant: search() must call "
            "HNSWIndexCache.get_or_load() exactly once for a fresh, "
            "uncached collection"
        )
        query_time_key = captured_query_time_keys[0]

        warm_time_key = store.hnsw_cache_key_for_collection(collection_path)

        assert warm_time_key == query_time_key, (
            f"Warm-time key {warm_time_key!r} (hnsw_cache_key_for_collection()) "
            f"diverged from the query-time key {query_time_key!r} that "
            "search() actually composed for the SAME collection -- this "
            "degrades daemon cache warming to a permanent miss "
            "(Bug #1736 Finding 1)."
        )
