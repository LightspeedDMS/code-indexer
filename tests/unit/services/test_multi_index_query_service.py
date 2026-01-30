"""
Unit tests for MultiIndexQueryService.

Tests the parallel multi-index query functionality that:
1. Detects if multimodal_index exists
2. Queries code_index and multimodal_index concurrently
3. Merges results with order-independent deduplication
4. Handles timeouts and partial results
5. Sorts by score descending
6. Provides query status metadata
"""

import pytest
from unittest.mock import Mock
import time

# Will create this module
from code_indexer.services.multi_index_query_service import MultiIndexQueryService


class TestMultiIndexQueryService:
    """Test MultiIndexQueryService functionality."""

    @pytest.fixture
    def mock_vector_store(self):
        """Create mock vector store client."""
        store = Mock()
        # search() returns tuple: (results, timing_info)
        store.search = Mock(return_value=([], {}))
        return store

    @pytest.fixture
    def mock_embedding_provider(self):
        """Create mock embedding provider."""
        provider = Mock()
        provider.embed_query = Mock(return_value=[0.1] * 1024)
        return provider

    @pytest.fixture
    def project_root(self, tmp_path):
        """Create temporary project root."""
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()

        # Create .code-indexer directory
        cidx_dir = project_dir / ".code-indexer"
        cidx_dir.mkdir()

        return project_dir

    def test_initialization(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test MultiIndexQueryService initialization."""
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        assert service.project_root == project_root
        assert service.vector_store == mock_vector_store
        assert service.embedding_provider == mock_embedding_provider

    def test_has_multimodal_index_when_exists(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test multimodal index detection when directory exists (LEGACY - subdirectory approach)."""
        # Create multimodal_index directory
        multimodal_dir = project_root / ".code-indexer" / "multimodal_index"
        multimodal_dir.mkdir(parents=True)

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        assert service.has_multimodal_index() is True

    def test_has_multimodal_collection_when_exists(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test multimodal collection detection when voyage-multimodal-3 collection exists (NEW architecture)."""
        # Create voyage-multimodal-3 collection directory (same level as voyage-code-3)
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Create a metadata file to mark collection as real
        meta_file = multimodal_collection / "collection_meta.json"
        meta_file.write_text('{"name": "voyage-multimodal-3"}')

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        assert service.has_multimodal_index() is True

    def test_has_multimodal_index_when_not_exists(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test multimodal index detection when directory does not exist."""
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        assert service.has_multimodal_index() is False

    def test_query_single_index_only(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test query when only code_index exists (backward compatibility)."""
        # No multimodal_index directory

        # Mock code_index results
        code_results = [
            {
                "id": "doc1",
                "score": 0.9,
                "payload": {
                    "path": "src/file1.py",
                    "chunk_offset": 0,
                    "content": "code content"
                }
            },
            {
                "id": "doc2",
                "score": 0.8,
                "payload": {
                    "path": "src/file2.py",
                    "chunk_offset": 0,
                    "content": "more code"
                }
            }
        ]

        mock_vector_store.search.return_value = (code_results, {})

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should query code_index only (once)
        assert mock_vector_store.search.call_count == 1

        # Should return code_index results
        assert len(results) == 2
        assert results[0]["score"] == 0.9
        assert results[1]["score"] == 0.8

    def test_query_multi_index_sequential(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test sequential query of voyage-code-3 then voyage-multimodal-3 collections."""
        # Create voyage-multimodal-3 collection directory (NEW architecture)
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Mock code_index results
        code_results = [
            {
                "id": "doc1",
                "score": 0.9,
                "payload": {
                    "path": "src/file1.py",
                    "chunk_offset": 0,
                    "content": "code content"
                }
            }
        ]

        # Mock multimodal_index results
        multimodal_results = [
            {
                "id": "doc2",
                "score": 0.85,
                "payload": {
                    "path": "docs/guide.md",
                    "chunk_offset": 0,
                    "content": "markdown content",
                    "images": ["diagram.png"]
                }
            }
        ]

        # Set up mock to return different results for different collection_name calls
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should query both indexes (2 calls)
        assert mock_vector_store.search.call_count == 2

        # Should merge and return both results
        assert len(results) == 2

    def test_result_merging_and_deduplication(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test result merging with deduplication by (file_path, chunk_offset)."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Mock code_index results - includes duplicate
        code_results = [
            {
                "id": "doc1",
                "score": 0.9,
                "payload": {
                    "path": "src/file1.py",
                    "chunk_offset": 0,
                    "content": "code content"
                }
            },
            {
                "id": "doc2",
                "score": 0.75,  # Lower score for same file/offset
                "payload": {
                    "path": "docs/guide.md",
                    "chunk_offset": 0,
                    "content": "markdown from code index"
                }
            }
        ]

        # Mock multimodal_index results - includes same file but higher score
        multimodal_results = [
            {
                "id": "doc3",
                "score": 0.85,  # Higher score for same file/offset
                "payload": {
                    "path": "docs/guide.md",
                    "chunk_offset": 0,
                    "content": "markdown from multimodal",
                    "images": ["diagram.png"]
                }
            },
            {
                "id": "doc4",
                "score": 0.8,
                "payload": {
                    "path": "docs/api.md",
                    "chunk_offset": 0,
                    "content": "api docs"
                }
            }
        ]

        # Set up mock to check collection_name instead of subdirectory
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should have 3 unique results (deduplicated docs/guide.md)
        assert len(results) == 3

        # Should keep higher score for duplicate
        guide_result = next(r for r in results if r["payload"]["path"] == "docs/guide.md")
        assert guide_result["score"] == 0.85  # Higher score from multimodal_index
        assert "images" in guide_result["payload"]  # Should preserve multimodal metadata

    def test_result_sorting_by_score_descending(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test that merged results are sorted by score descending."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Mock results with different scores
        code_results = [
            {"id": "doc1", "score": 0.7, "payload": {"path": "file1.py", "chunk_offset": 0}},
            {"id": "doc2", "score": 0.9, "payload": {"path": "file2.py", "chunk_offset": 0}}
        ]

        multimodal_results = [
            {"id": "doc3", "score": 0.95, "payload": {"path": "file3.md", "chunk_offset": 0}},
            {"id": "doc4", "score": 0.6, "payload": {"path": "file4.md", "chunk_offset": 0}}
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should be sorted by score descending
        assert len(results) == 4
        assert results[0]["score"] == 0.95
        assert results[1]["score"] == 0.9
        assert results[2]["score"] == 0.7
        assert results[3]["score"] == 0.6

    def test_limit_applied_to_merged_results(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test that limit is applied to merged results."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Mock multiple results
        code_results = [
            {"id": f"doc{i}", "score": 0.9 - i*0.1, "payload": {"path": f"file{i}.py", "chunk_offset": 0}}
            for i in range(5)
        ]

        multimodal_results = [
            {"id": f"mdoc{i}", "score": 0.85 - i*0.1, "payload": {"path": f"file{i}.md", "chunk_offset": 0}}
            for i in range(5)
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=3,
            collection_name="code_index"
        )

        # Should return only top 3 results
        assert len(results) == 3
        assert results[0]["score"] == 0.9  # Highest score
        assert results[1]["score"] == 0.85
        assert results[2]["score"] == 0.8

    def test_filter_conditions_passed_to_both_indexes(self, project_root, mock_vector_store, mock_embedding_provider):
        """Test that filter conditions are passed to both index queries."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        mock_vector_store.search.return_value = ([], {})

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        filter_conditions = {
            "must": [{"key": "language", "match": {"value": "python"}}]
        }

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index",
            filter_conditions=filter_conditions
        )

        # Should have called search twice
        assert mock_vector_store.search.call_count == 2

        # Both calls should have filter conditions
        for call in mock_vector_store.search.call_args_list:
            assert call[1]["filter_conditions"] == filter_conditions

    # ========== NEW TESTS FOR PARALLEL EXECUTION (Story #65) ==========

    def test_parallel_query_execution_both_indexes_called_concurrently(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Test that both index queries execute in parallel, not sequentially."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        def slow_search(*args, **kwargs):
            """Simulate slow search that takes 0.1 seconds."""
            time.sleep(0.1)

            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return ([{"id": "code", "score": 0.9, "payload": {"path": "a.py", "chunk_offset": 0}}], {})
            elif collection == "voyage-multimodal-3":
                return ([{"id": "mm", "score": 0.8, "payload": {"path": "b.md", "chunk_offset": 0}}], {})
            return ([], {})

        mock_vector_store.search.side_effect = slow_search

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        start_time = time.time()
        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )
        total_time = time.time() - start_time

        # Parallel execution: total time should be ~0.1s (max of both), not ~0.2s (sum)
        assert total_time < 0.15, f"Expected parallel execution (~0.1s), got {total_time:.3f}s"

        # Both queries should have been called
        assert mock_vector_store.search.call_count == 2

        # Both results should be merged (return type is list for backward compatibility)
        assert isinstance(results, list)
        assert len(results) == 2

    def test_merge_order_independence(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Test that merge produces consistent results regardless of which query finishes first."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        # Same duplicate result in both indexes
        code_results = [
            {"id": "c1", "score": 0.9, "payload": {"path": "file.py", "chunk_offset": 0}},
            {"id": "c2", "score": 0.75, "payload": {"path": "dup.md", "chunk_offset": 0}},
        ]

        multimodal_results = [
            {"id": "m1", "score": 0.85, "payload": {"path": "dup.md", "chunk_offset": 0}},
            {"id": "m2", "score": 0.8, "payload": {"path": "guide.md", "chunk_offset": 0}},
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should return list (backward compatible)
        assert isinstance(results, list)

        # Should have 3 unique results (deduplicated dup.md)
        assert len(results) == 3

        # Should keep higher score for duplicate
        dup_result = next(r for r in results if r["payload"]["path"] == "dup.md")
        assert dup_result["score"] == 0.85  # Higher score from multimodal

        # Should be sorted by score descending
        assert results[0]["score"] == 0.9
        assert results[1]["score"] == 0.85
        assert results[2]["score"] == 0.8

    def test_timeout_handling_one_slow_query(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Test that timeout on one index returns partial results from successful index."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        code_results = [
            {"id": "c1", "score": 0.9, "payload": {"path": "file.py", "chunk_offset": 0}}
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                # Code index succeeds quickly
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                # Multimodal index times out - simulate with exception
                from concurrent.futures import TimeoutError
                raise TimeoutError("Query timeout")
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        # Query should return code results even if multimodal times out
        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should return list (backward compatible)
        assert isinstance(results, list)

        # Should have code_index results even though multimodal failed
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "file.py"

    def test_timeout_returns_partial_results(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Test that when code_index times out, multimodal results are still returned."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        multimodal_results = [
            {"id": "m1", "score": 0.85, "payload": {"path": "guide.md", "chunk_offset": 0}}
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                # Code index times out
                from concurrent.futures import TimeoutError
                raise TimeoutError("Query timeout")
            elif collection == "voyage-multimodal-3":
                # Multimodal succeeds
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should return list (backward compatible)
        assert isinstance(results, list)

        # Should have multimodal results even though code_index failed
        assert len(results) == 1
        assert results[0]["payload"]["path"] == "guide.md"

    def test_backward_compatibility_same_results(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Test that parallel execution produces same results as sequential (backward compatibility)."""
        # Create voyage-multimodal-3 collection directory
        multimodal_collection = project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        multimodal_collection.mkdir(parents=True)

        code_results = [
            {"id": "c1", "score": 0.95, "payload": {"path": "file1.py", "chunk_offset": 0}},
            {"id": "c2", "score": 0.75, "payload": {"path": "file2.py", "chunk_offset": 0}},
        ]

        multimodal_results = [
            {"id": "m1", "score": 0.85, "payload": {"path": "file3.md", "chunk_offset": 0}},
            {"id": "m2", "score": 0.65, "payload": {"path": "file4.md", "chunk_offset": 0}},
        ]

        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name")
            if collection == "code_index" or collection == "voyage-code-3":
                return (code_results, {})
            elif collection == "voyage-multimodal-3":
                return (multimodal_results, {})
            return ([], {})

        mock_vector_store.search.side_effect = search_side_effect

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider
        )

        results, timing = service.query(
            query_text="test query",
            limit=10,
            collection_name="code_index"
        )

        # Should return list (backward compatible)
        assert isinstance(results, list)

        # Should have all 4 results
        assert len(results) == 4

        # Should be sorted by score descending (same as sequential)
        assert results[0]["score"] == 0.95
        assert results[1]["score"] == 0.85
        assert results[2]["score"] == 0.75
        assert results[3]["score"] == 0.65
