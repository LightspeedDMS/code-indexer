"""Test for commit message search limit bug.

Bug: Commit message searches return max 3 results despite higher --limit values.

Root Cause: FilesystemVectorStore.search() ignores prefetch_limit parameter
and uses k=limit*2 for HNSW queries, causing insufficient candidates when
filters are applied.

Reproduction:
- 764 commit message vectors indexed
- Query with --limit 20 --chunk-type commit_message
- Expected: Up to 20 results
- Actual: Max 3 results

Evidence:
- HNSW asked for k=limit*2 candidates instead of k=prefetch_limit
- prefetch_limit parameter passed but never used
- Filters reduce candidate pool significantly
"""

import pytest
from pathlib import Path
from unittest.mock import Mock

VECTOR_DIM = 1024


def _make_mock_hnsw_manager() -> Mock:
    """A mocked HNSW manager: genuinely external/heavy (avoids building a
    real hnswlib index just to assert the k= parameter passed to query())."""
    mock_hnsw_manager = Mock()
    mock_hnsw_manager.is_stale.return_value = False
    mock_hnsw_manager.load_index.return_value = Mock()
    mock_hnsw_manager.query.return_value = ([], [])
    return mock_hnsw_manager


def test_search_uses_prefetch_limit_not_limit_multiplier(tmp_path: Path, monkeypatch):
    """FAILING TEST: search() should use prefetch_limit for HNSW k parameter.

    This test demonstrates the bug where prefetch_limit is ignored and
    limit*2 is used instead, causing insufficient candidates when filters applied.

    Story #1492 AC1: search() now reads collection_meta.json via the real
    CollectionMetaCache (mtime-keyed) instead of a bare open()+json.load()
    call, so this test uses the real store.create_collection() (writing a
    genuine collection_meta.json to a real tmp_path directory) instead of
    mocking builtins.open/json.load/the internal _id_index dict -- those
    mocks either can no longer intercept the read (open/json.load) or
    substituted internal state of the system under test (_id_index), which
    this project's Anti-Mock standard reserves for genuinely external/heavy
    dependencies only (here: the HNSW manager and the embedding provider).
    With no vector files ever written, _load_id_index()'s real directory-
    scan fallback naturally returns an empty dict -- no mock needed.
    """
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
    from code_indexer.storage import hnsw_index_manager as hnsw_index_manager_mod

    base_path = tmp_path / "test_index"
    store = FilesystemVectorStore(base_path=base_path)
    assert store.create_collection("test_collection", vector_size=VECTOR_DIM)

    mock_hnsw_manager = _make_mock_hnsw_manager()
    monkeypatch.setattr(
        hnsw_index_manager_mod,
        "HNSWIndexManager",
        lambda *a, **kw: mock_hnsw_manager,
    )

    mock_embedding_provider = Mock()
    mock_embedding_provider.get_embedding.return_value = [0.1] * VECTOR_DIM

    user_limit = 20
    prefetch_limit = 400  # Over-fetch for filters

    store.search(
        query="fix",
        embedding_provider=mock_embedding_provider,
        collection_name="test_collection",
        limit=user_limit,
        lazy_load=True,
        prefetch_limit=prefetch_limit,
    )

    # CRITICAL ASSERTION: HNSW should be queried with prefetch_limit, not limit*2
    mock_hnsw_manager.query.assert_called_once()
    call_kwargs = mock_hnsw_manager.query.call_args[1]

    # BUG: Currently uses k=limit*2 (=40) instead of k=prefetch_limit (=400)
    assert call_kwargs["k"] == prefetch_limit, (
        f"HNSW query should use k=prefetch_limit ({prefetch_limit}), "
        f"but used k={call_kwargs['k']} (limit*2={user_limit * 2})"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
