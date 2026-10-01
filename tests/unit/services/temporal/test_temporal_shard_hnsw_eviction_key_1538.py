"""Bug #1538: the temporal dispatch's post-shard HNSW eviction must actually evict.

``_query_shards_raw`` evicts each temporal shard's HNSW entry from the shared
server cache after reading it (Bug #1171's proven-safe baseline, made
conditional on the MemoryGovernor by Story #1213). That eviction composed its
key as a bare ``base_path/shard_name`` path string -- but Story #1458 AC11
changed what ``search()`` STORES under: ``path:{chunk_layout_token}``. The two
keys have not matched since, so the eviction has been a silent no-op and every
temporal shard's graph has lingered in every worker's cache regardless of the
governor's decision. AC11 documented this exact hazard and fixed it for
``rebuild_hnsw_filtered()``'s two ``invalidate()`` calls; this call site was
missed.

That lingering entry is what Bug #1529's fixed-path design then made
observable as indefinite post-refresh staleness, so eviction landing on the
right key is part of that fix, not a separate cleanup.

Proven end to end against the REAL write path, the REAL ``search()`` read path
and the REAL eviction helper the dispatch loop calls -- real filesystem, real
SQLite chunk store, real hnswlib graph, nothing mocked.

Typing note: ``Dict[str, Any]`` is not a loose annotation here -- it is the
literal, heterogeneous public contract of the code under test.
``FilesystemVectorStore.upsert_points()`` accepts ``List[Dict[str, Any]]`` and
``search()`` returns ``List[Dict[str, Any]]``; a record mixes an id string, a
float vector, a nested payload dict and chunk text, so there is no narrower
declared type available to name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from code_indexer.server.cache.hnsw_index_cache import (
    HNSWIndexCache,
    HNSWIndexCacheConfig,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    evict_shard_hnsw_entry,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

SHARD_NAME = "code-indexer-temporal-voyage_code_3-2026Q3"
VECTOR_SIZE = 8
CACHE_TTL_MINUTES = 60
SEARCH_LIMIT = 10
ROW_COUNT = 3


def _record(i: int) -> Dict[str, Any]:
    rng = np.random.default_rng(1538 + i)
    return {
        "id": f"proj:commit:{i:08x}:0",
        "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
        "payload": {"path": f"src/f{i}.py", "commit_hash": f"{i:08x}"},
        "chunk_text": f"content {i}",
    }


class _UnusedProvider:
    """Never called: the search below passes precomputed_query_vector."""


@pytest.fixture
def index_root(tmp_path: Path) -> Path:
    """A real temporal index root holding one real, populated CHUNKS_DB shard.

    Written through the ACTUAL temporal write sequence (create_collection ->
    begin_indexing -> upsert_points -> end_indexing) at a fixed root, so the
    committed chunk-layout discriminator is genuine rather than hand-forged.
    """
    root = tmp_path / ".temporal" / "e2e2"
    root.mkdir(parents=True)
    writer = FilesystemVectorStore(
        base_path=root, use_chunks_db_for_new_collections=True
    )
    writer.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    writer.begin_indexing(SHARD_NAME)
    writer.upsert_points(SHARD_NAME, [_record(i) for i in range(ROW_COUNT)])
    writer.end_indexing(SHARD_NAME)
    return root


def test_post_shard_eviction_removes_the_entry_search_stored(index_root: Path) -> None:
    cache = HNSWIndexCache(HNSWIndexCacheConfig(ttl_minutes=CACHE_TTL_MINUTES))
    store = FilesystemVectorStore(base_path=index_root, hnsw_index_cache=cache)

    # search() returns either a bare result list or a (results, timing) tuple
    # depending on return_timing; with the default it is the list, narrowed
    # below rather than suppressed with a type-check escape.
    results = store.search(
        query="probe",
        embedding_provider=_UnusedProvider(),
        collection_name=SHARD_NAME,
        limit=SEARCH_LIMIT,
        precomputed_query_vector=_record(0)["vector"],
    )
    assert isinstance(results, list)
    assert len(results) == ROW_COUNT, "read path must return the shard's real rows"
    assert cache.get_stats().cached_repositories == 1, (
        "search() must have populated the shared HNSW cache for this shard"
    )

    evict_shard_hnsw_entry(store, SHARD_NAME)

    assert cache.get_stats().cached_repositories == 0, (
        "the temporal dispatch's post-shard eviction did not remove the entry "
        "search() stored -- its key omits the chunk-layout token search() "
        "embeds, making every temporal shard eviction a silent no-op (Bug #1538)"
    )
