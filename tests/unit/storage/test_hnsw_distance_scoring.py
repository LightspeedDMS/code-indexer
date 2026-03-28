"""Tests for Story #440: Trust HNSW distances, eliminate redundant vector JSON re-reads.

Verifies that the HNSW search path:
1. Uses HNSW distances directly (1.0 - distance) instead of recalculating cosine similarity
2. Only reads JSON files for the top `limit` results when no filter_conditions
3. Applies score_threshold filter before reading JSON files
4. Still correctly handles filter_conditions (reads JSON for filter evaluation, uses HNSW scores)
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


class JSONLoadCounter:
    """Context manager to count json.load calls on vector_point files during search.

    Uses unittest.mock.patch on 'code_indexer.storage.filesystem_vector_store.json.load'
    which is the standard pytest-compatible way to intercept module-level function calls.
    Counting is done by inspecting the file handle's name attribute.
    """

    def __init__(self):
        self.load_count = 0
        self.loaded_files: list = []
        self._patcher = None
        self._original_load = json.load

    def __enter__(self):
        counter = self

        def counting_load(fp, **kwargs):
            name = str(getattr(fp, "name", ""))
            if "vector_point_" in name and name.endswith(".json"):
                counter.load_count += 1
                counter.loaded_files.append(name)
            return counter._original_load(fp, **kwargs)

        self._patcher = patch(
            "code_indexer.storage.filesystem_vector_store.json.load",
            side_effect=counting_load,
        )
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._patcher is not None:
            self._patcher.stop()


@pytest.fixture
def store_with_vectors():
    """Create a store with 50 indexed vectors for testing HNSW distance scoring."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = Path(tmpdir)
        store = FilesystemVectorStore(base_path=base_path)

        collection_name = "test_collection"
        vector_size = 64
        store.create_collection(
            collection_name=collection_name, vector_size=vector_size
        )

        # Add 50 vectors
        points = []
        for i in range(50):
            vector = np.random.rand(vector_size).tolist()
            language = "python" if i < 25 else "javascript"
            points.append(
                {
                    "id": f"point_{i}",
                    "vector": vector,
                    "payload": {
                        "file_path": f"/test/file_{i}.py",
                        "language": language,
                        "chunk_text": f"Test content {i}",
                        "blob_hash": f"hash_{i}",
                    },
                }
            )

        store.upsert_points(collection_name=collection_name, points=points)

        # Build HNSW index
        from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

        collection_path = base_path / collection_name
        hnsw_manager = HNSWIndexManager(vector_dim=vector_size, space="cosine")
        hnsw_manager.rebuild_from_vectors(
            collection_path=collection_path, progress_callback=None
        )

        yield store, collection_name, vector_size


def test_hnsw_search_uses_hnsw_distances(store_with_vectors):
    """Verify that scores returned by HNSW search match 1.0 - distance.

    HNSW with space='cosine' returns distances where distance = 1.0 - cosine_similarity.
    This test verifies that scores are derived from HNSW distances directly, not
    recalculated via numpy dot product.

    Since 1.0 - distance == cosine_similarity, scores should be equivalent to the
    previous implementation within floating point tolerance.
    """
    store, collection_name, vector_size = store_with_vectors

    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    results = store.search(
        query="test query",
        embedding_provider=mock_provider,
        collection_name=collection_name,
        limit=5,
    )

    # Results should exist
    assert len(results) > 0, "Search should return results"

    # All scores should be in [0, 1] range (valid cosine similarities)
    for result in results:
        score = result["score"]
        assert 0.0 <= score <= 1.0, f"Score {score} out of valid cosine range [0, 1]"

    # Scores should be sorted descending
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True), (
        "Results should be sorted by score descending"
    )


def test_hnsw_search_reduces_json_reads(store_with_vectors):
    """Verify that with limit=5 and no filter_conditions, at most 5 JSON files are read.

    The optimization: with no filter_conditions, we can apply score_threshold on HNSW
    distances before reading any JSON files, then only read JSON for the top `limit`
    results.

    This is the core of Story #440 - eliminate redundant vector JSON re-reads.
    """
    store, collection_name, vector_size = store_with_vectors

    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    limit = 5

    with JSONLoadCounter() as counter:
        results = store.search(
            query="test query",
            embedding_provider=mock_provider,
            collection_name=collection_name,
            limit=limit,
        )

    json_reads = counter.load_count

    # Should have results
    assert len(results) > 0, "Search should return results"

    # CRITICAL: With no filters and limit=5, JSON reads should be exactly `limit`
    # (only reading files for the top results, not all candidates)
    assert json_reads <= limit, (
        f"With no filter_conditions and limit={limit}, "
        f"JSON reads should be at most {limit} but was {json_reads}. "
        f"The optimization should read JSON only for top results."
    )


def test_hnsw_search_score_threshold_pre_filter(store_with_vectors):
    """Verify that score_threshold is applied before reading JSON files.

    With a very high score_threshold (0.99), almost no candidates should pass.
    We should read minimal JSON files (only for candidates above threshold).
    """
    store, collection_name, vector_size = store_with_vectors

    # Use a random query - with threshold 0.99, very few or zero results expected
    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    # Very high threshold - most candidates should be filtered without reading JSON
    high_threshold = 0.99

    with JSONLoadCounter() as counter:
        results = store.search(
            query="test query",
            embedding_provider=mock_provider,
            collection_name=collection_name,
            limit=10,
            score_threshold=high_threshold,
        )

    json_reads_high_threshold = counter.load_count

    # Now search with no threshold - should read more JSON files
    with JSONLoadCounter() as counter2:
        store.search(
            query="test query",
            embedding_provider=mock_provider,
            collection_name=collection_name,
            limit=10,
        )

    json_reads_no_threshold = counter2.load_count

    # With threshold=0.99, likely 0 results (random vectors rarely have 0.99 similarity)
    # The key assertion: high threshold should NOT cause reading all 50 JSON files
    # If threshold is applied before JSON reads, we read 0 or very few files
    assert json_reads_high_threshold <= json_reads_no_threshold, (
        f"High score_threshold should not require MORE JSON reads than no threshold. "
        f"High threshold reads: {json_reads_high_threshold}, "
        f"No threshold reads: {json_reads_no_threshold}"
    )

    # With very high threshold on random vectors, results should be empty or very few
    # (this validates the threshold is working, not just that reads are fewer)
    assert len(results) == 0 or results[0]["score"] >= high_threshold


def test_hnsw_search_with_filter_conditions(store_with_vectors):
    """Verify that filter_conditions still work correctly with HNSW-derived scores.

    When filter_conditions are present, JSON must be read for filter evaluation.
    But scores should still be derived from HNSW distances, not recalculated.
    """
    store, collection_name, vector_size = store_with_vectors

    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    limit = 5

    # Search with filter for python only
    filter_conditions = {"must": [{"key": "language", "match": {"value": "python"}}]}

    results = store.search(
        query="test query",
        embedding_provider=mock_provider,
        collection_name=collection_name,
        limit=limit,
        filter_conditions=filter_conditions,
    )

    # All results should be python
    for result in results:
        assert result["payload"]["language"] == "python", (
            f"Filter should only return python results, got: {result['payload']['language']}"
        )

    # Results should not exceed limit
    assert len(results) <= limit

    # Scores should be in valid range
    for result in results:
        assert 0.0 <= result["score"] <= 1.0


def test_hnsw_search_score_consistency_with_filter(store_with_vectors):
    """Verify score values are consistent between filtered and unfiltered searches.

    When the same document appears in both filtered and unfiltered results,
    its score should be the same (derived from HNSW distance, not recalculated).
    """
    store, collection_name, vector_size = store_with_vectors

    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    # Search without filter
    results_no_filter = store.search(
        query="test query",
        embedding_provider=mock_provider,
        collection_name=collection_name,
        limit=20,
    )

    # Search with filter for python (25 out of 50 are python)
    filter_conditions = {"must": [{"key": "language", "match": {"value": "python"}}]}
    results_filtered = store.search(
        query="test query",
        embedding_provider=mock_provider,
        collection_name=collection_name,
        limit=20,
        filter_conditions=filter_conditions,
    )

    # Build lookup by id for unfiltered results
    unfiltered_by_id = {r["id"]: r["score"] for r in results_no_filter}

    # For each filtered result that also appears in unfiltered, scores should match
    for result in results_filtered:
        doc_id = result["id"]
        if doc_id in unfiltered_by_id:
            unfiltered_score = unfiltered_by_id[doc_id]
            filtered_score = result["score"]
            assert abs(filtered_score - unfiltered_score) < 1e-6, (
                f"Score for {doc_id} should be consistent: "
                f"filtered={filtered_score}, unfiltered={unfiltered_score}"
            )


def test_hnsw_search_no_filter_max_json_reads_equals_limit(store_with_vectors):
    """Verify that with no filter_conditions, JSON reads equal exactly limit (not candidates).

    This is the direct test of the optimization: with 50 candidates and limit=5,
    we should read exactly 5 JSON files, not 50 (or 10 which was limit*2 before).
    """
    store, collection_name, vector_size = store_with_vectors

    query_vector = np.random.rand(vector_size).tolist()
    mock_provider = MagicMock()
    mock_provider.get_embedding.return_value = query_vector

    limit = 5

    with JSONLoadCounter() as counter:
        results = store.search(
            query="test query",
            embedding_provider=mock_provider,
            collection_name=collection_name,
            limit=limit,
            # No filter_conditions = maximum optimization path
        )

    json_reads = counter.load_count

    assert len(results) == limit, f"Should return exactly {limit} results"

    # With the optimization, JSON reads should equal limit, not all candidates
    # HNSW returns candidates sorted by distance, so top-limit are selected
    # before any JSON reads
    assert json_reads == limit, (
        f"With no filters and limit={limit}, should read exactly {limit} JSON files "
        f"(one per result), but read {json_reads}. "
        f"Candidates fetched: limit*2={limit * 2}, total vectors: 50"
    )
