"""Story #1493 AC2: rows a temporal chunk_type post-filter would discard must
never be fully decoded (zstd + json.loads) via ChunkStore.read().

Report Finding C2: for a chunk_type=commit_message query, up to 1,200 rows
per shard were fully hydrated even though ~97.3% are non-head (commit_diff)
chunks the caller's own is_head post-filter (_filter_by_time_range) discards
anyway. FilesystemVectorStore.search()'s new `temporal_chunk_type` parameter
lets the CHUNKS_DB hydration loop skip ChunkStore.read() entirely for any
HNSW candidate whose point_id parses as the OPPOSITE chunk type -- derived
with zero I/O from the point_id string alone (temporal_point_builder.py's
is_head_chunk_id()).

Real ChunkStore, real HNSW build, real FilesystemVectorStore.search() -- no
mocking of the code under test, per this project's anti-mock rule.
"""

from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.services.temporal.temporal_point_builder import build_point_id
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_DIM = 32
# Mirrors the report's real-world skew: commit_message (head, chunk_index 0)
# is rare; commit_diff (non-head) chunks dominate.
_NUM_COMMITS = 60
_CHUNKS_PER_COMMIT = 5  # 1 head (message) + 4 non-head (diff) chunks


def _build_temporal_chunks_db_collection(
    store: FilesystemVectorStore,
    collection_name: str,
    vectors: list,
) -> Path:
    """Build a real CHUNKS_DB collection whose point_ids follow the unified
    temporal scheme ("{project}:commit:{hash}:{j}"), j==0 == head/message."""
    store.create_collection(collection_name, vector_size=VECTOR_DIM)
    collection_path = Path(store._get_collection_path(collection_name))

    records = []
    idx = 0
    for commit_i in range(_NUM_COMMITS):
        commit_hash = f"hash{commit_i:04d}"
        for chunk_index in range(_CHUNKS_PER_COMMIT):
            point_id = build_point_id("proj", commit_hash, chunk_index)
            records.append(
                {
                    "id": point_id,
                    "vector": vectors[idx].astype(np.float32).tolist(),
                    "payload": {
                        "path": f"{commit_hash}.py",
                        "is_head": chunk_index == 0,
                        "commit_timestamp": 1700000000 + commit_i,
                    },
                    "chunk_text": f"chunk text for {point_id}",
                }
            )
            idx += 1

    chunk_store = ChunkStore(collection_path / "chunks.db")
    try:
        chunk_store.write_batch(records)
    finally:
        chunk_store.close()

    write_chunks_db_discriminator(collection_path)

    hnsw_manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
    hnsw_manager.rebuild_from_vectors(collection_path)

    return collection_path


@pytest.fixture
def rng():
    return np.random.default_rng(4242)


def test_commit_message_query_skips_decode_for_non_head_candidates(tmp_path, rng):
    """chunk_type='commit_message' must skip ChunkStore.read() for every
    non-head candidate WITHOUT decoding it -- decode count must be bounded
    by (head candidates considered), never by the full overfetch pool."""
    store = FilesystemVectorStore(base_path=tmp_path)
    total_vectors = _NUM_COMMITS * _CHUNKS_PER_COMMIT
    vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(total_vectors)]
    _build_temporal_chunks_db_collection(store, "coll", vectors)

    # Mirrors query_temporal's ALWAYS-present commit_timestamp `must` range
    # filter (near-trivial: covers everything), forcing the Case B path.
    filter_conditions = {
        "must": [
            {
                "key": "commit_timestamp",
                "range": {"gte": 0, "lte": 9999999999},
            }
        ]
    }

    # Emulates the report's worst case: a large overfetch pool (search_limit)
    # for a small user-requested limit, via chunk_type=commit_message.
    limit = 10

    with patch.object(
        ChunkStore, "read", autospec=True, side_effect=ChunkStore.read
    ) as read_spy:
        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=limit,
            filter_conditions=filter_conditions,
            precomputed_query_vector=vectors[0].tolist(),
            lazy_load=True,
            prefetch_limit=total_vectors,  # every vector is a candidate
            temporal_chunk_type="commit_message",
        )

    # Only _NUM_COMMITS candidates are actually head chunks -- every
    # non-head candidate must be skipped WITHOUT a ChunkStore.read() call.
    assert read_spy.call_count <= _NUM_COMMITS
    # Surviving results must all be head chunks (chunk_index 0).
    assert len(results) > 0
    assert all(r["payload"]["is_head"] is True for r in results)


def test_commit_diff_returns_both_head_and_non_head_chunks(tmp_path, rng):
    """chunk_type='commit_diff' has REAL semantics of "keep ALL chunks, no
    is_head filtering at all" (temporal_search_service.py's
    _filter_by_time_range: "commit_diff keeps ALL chunks (no filtering)").
    A naive "wants_head = (chunk_type == 'commit_message')" implementation
    would WRONGLY treat every commit_diff query as "discard all head
    chunks" -- this must not happen: results must include BOTH head and
    non-head candidates when ranked highly enough, never silently missing
    every head chunk."""
    store = FilesystemVectorStore(base_path=tmp_path)
    total_vectors = _NUM_COMMITS * _CHUNKS_PER_COMMIT
    vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(total_vectors)]
    _build_temporal_chunks_db_collection(store, "coll", vectors)

    filter_conditions = {
        "must": [
            {
                "key": "commit_timestamp",
                "range": {"gte": 0, "lte": 9999999999},
            }
        ]
    }

    results = store.search(
        query="unused",
        embedding_provider=Mock(),
        collection_name="coll",
        limit=total_vectors,
        filter_conditions=filter_conditions,
        precomputed_query_vector=vectors[0].tolist(),
        lazy_load=False,
        prefetch_limit=total_vectors,
        temporal_chunk_type="commit_diff",
    )

    assert len(results) == total_vectors
    head_count = sum(1 for r in results if r["payload"]["is_head"] is True)
    non_head_count = sum(1 for r in results if r["payload"]["is_head"] is False)
    assert head_count == _NUM_COMMITS
    assert non_head_count == _NUM_COMMITS * (_CHUNKS_PER_COMMIT - 1)


def test_surviving_rows_are_byte_identical_to_full_decode(tmp_path, rng):
    """Rows that DO survive (head chunks, for chunk_type=commit_message)
    must hydrate to exactly the same content as the pre-#1493 full-decode
    path -- proven by comparing against a plain query with no
    temporal_chunk_type filter (which decodes everything, including the
    same head rows)."""
    store = FilesystemVectorStore(base_path=tmp_path)
    total_vectors = _NUM_COMMITS * _CHUNKS_PER_COMMIT
    vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(total_vectors)]
    _build_temporal_chunks_db_collection(store, "coll", vectors)

    filter_conditions = {
        "must": [
            {
                "key": "commit_timestamp",
                "range": {"gte": 0, "lte": 9999999999},
            }
        ]
    }
    limit = 10
    common_kwargs = dict(
        query="unused",
        embedding_provider=Mock(),
        collection_name="coll",
        filter_conditions=filter_conditions,
        precomputed_query_vector=vectors[0].tolist(),
        lazy_load=False,
        prefetch_limit=total_vectors,
    )

    results_with_projection = store.search(
        limit=limit, temporal_chunk_type="commit_message", **common_kwargs
    )
    # Baseline uses a LARGE limit (not the small user-facing `limit`) so the
    # comparison set isn't truncated to an unrelated top-N-overall subset
    # before we can look up each surviving projected row's counterpart.
    results_without_projection = store.search(limit=total_vectors, **common_kwargs)

    head_ids_from_full_decode = {
        r["id"]
        for r in results_without_projection
        if r["payload"].get("is_head") is True
    }
    assert head_ids_from_full_decode  # sanity: some head rows exist

    for result in results_with_projection:
        matching = [r for r in results_without_projection if r["id"] == result["id"]]
        assert len(matching) == 1
        assert result == matching[0]
