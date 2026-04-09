"""Tests for Bug #668: HNSW must NOT be rebuilt inline during query when stale.

The search path must never call rebuild_from_vectors(). When HNSW is stale:
  - If hnsw_index.bin exists on disk → use it as-is (with warning log)
  - If hnsw_index.bin is missing    → return empty results (with warning log)

These tests fail before the fix (rebuild_from_vectors is called) and pass after.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


@pytest.fixture
def temp_store():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "index"
        base.mkdir()
        store = FilesystemVectorStore(base_path=base)
        yield store, base


def _make_collection(base: Path, collection_name: str, stale: bool, has_bin: bool):
    """Create a minimal collection directory with metadata and optional HNSW bin.

    Args:
        base: base_path of the FilesystemVectorStore (collections live directly inside)
        collection_name: e.g. 'my-collection'
        stale: whether to mark is_stale=True in hnsw metadata
        has_bin: whether to place a (fake) hnsw_index.bin file
    """
    coll_dir = base / collection_name
    coll_dir.mkdir(parents=True)

    # Minimal collection_meta.json
    meta = {"vector_size": 4, "model": "test-model", "total_vectors": 1}
    (coll_dir / "collection_meta.json").write_text(json.dumps(meta))

    # HNSW metadata file (hnsw_metadata.json)
    hnsw_meta = {
        "hnsw_index": {
            "is_stale": stale,
            "total_vectors": 1,
            "index_file_size": 100 if has_bin else 0,
        }
    }
    (coll_dir / "hnsw_metadata.json").write_text(json.dumps(hnsw_meta))

    if has_bin:
        # Place a placeholder file so index_file.exists() returns True
        (coll_dir / "hnsw_index.bin").write_bytes(b"\x00" * 16)

    return coll_dir


def _mock_embedding_provider(vector=None):
    """Return a mock embedding provider that returns a fixed 4-dim vector."""
    if vector is None:
        vector = [0.1, 0.2, 0.3, 0.4]
    mock = MagicMock()
    mock.embed_query.return_value = vector
    mock.embed.return_value = [vector]
    return mock


class TestStaleHNSWQueryNeverRebuilds:
    """Bug #668: rebuild_from_vectors must never be called during a search."""

    def test_stale_hnsw_with_bin_does_not_call_rebuild(self, temp_store):
        """When HNSW is stale but the bin file exists, search must NOT rebuild.
        It should use the existing index as-is.
        """
        store, base = temp_store
        _make_collection(base, "test-coll", stale=True, has_bin=True)

        embedding_provider = _mock_embedding_provider()

        with patch(
            "code_indexer.storage.hnsw_index_manager.HNSWIndexManager.rebuild_from_vectors"
        ) as mock_rebuild:
            # Also patch load_index to avoid loading a real (invalid) HNSW file
            with patch(
                "code_indexer.storage.hnsw_index_manager.HNSWIndexManager.load_index",
                return_value=MagicMock(
                    get_ids_batch=MagicMock(return_value=[]),
                    knn_query=MagicMock(return_value=([], [])),
                ),
            ):
                store.search(
                    query="test query",
                    embedding_provider=embedding_provider,
                    collection_name="test-coll",
                    limit=5,
                )

        (
            mock_rebuild.assert_not_called(),
            (
                "Bug #668: rebuild_from_vectors was called during search with stale "
                "but existing HNSW. Searches must never trigger index rebuilds."
            ),
        )

    def test_stale_hnsw_missing_bin_returns_empty_not_rebuild(self, temp_store):
        """When HNSW is stale AND the bin file is missing, search must return empty
        without calling rebuild_from_vectors.
        """
        store, base = temp_store
        _make_collection(base, "test-coll-nobin", stale=True, has_bin=False)

        embedding_provider = _mock_embedding_provider()

        with patch(
            "code_indexer.storage.hnsw_index_manager.HNSWIndexManager.rebuild_from_vectors"
        ) as mock_rebuild:
            results = store.search(
                query="test query",
                embedding_provider=embedding_provider,
                collection_name="test-coll-nobin",
                limit=5,
            )

        (
            mock_rebuild.assert_not_called(),
            (
                "Bug #668: rebuild_from_vectors was called during search with stale "
                "and missing HNSW. Searches must never trigger index rebuilds."
            ),
        )
        assert results == [], (
            "When HNSW is stale and missing, search must return empty results."
        )

    def test_fresh_hnsw_does_not_call_rebuild(self, temp_store):
        """Control: fresh HNSW also must not rebuild (baseline sanity check)."""
        store, base = temp_store
        _make_collection(base, "fresh-coll", stale=False, has_bin=True)

        embedding_provider = _mock_embedding_provider()

        with patch(
            "code_indexer.storage.hnsw_index_manager.HNSWIndexManager.rebuild_from_vectors"
        ) as mock_rebuild:
            with patch(
                "code_indexer.storage.hnsw_index_manager.HNSWIndexManager.load_index",
                return_value=MagicMock(
                    get_ids_batch=MagicMock(return_value=[]),
                    knn_query=MagicMock(return_value=([], [])),
                ),
            ):
                store.search(
                    query="test query",
                    embedding_provider=embedding_provider,
                    collection_name="fresh-coll",
                    limit=5,
                )

        mock_rebuild.assert_not_called()
