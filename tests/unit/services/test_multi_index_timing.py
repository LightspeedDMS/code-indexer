"""
Tests for MultiIndexQueryService timing capture and aggregation.

These tests verify that the MultiIndexQueryService properly captures,
aggregates, and returns timing information for parallel multi-index queries.
"""

import pytest
from unittest.mock import Mock, patch
from src.code_indexer.services.multi_index_query_service import MultiIndexQueryService


class TestMultiIndexTiming:
    """Test timing capture and aggregation in MultiIndexQueryService."""

    @pytest.fixture
    def mock_project_root(self, tmp_path):
        """Create mock project root with .code-indexer directory."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        code_indexer_dir = project_root / ".code-indexer"
        code_indexer_dir.mkdir()
        return project_root

    @pytest.fixture
    def mock_vector_store(self):
        """Create mock vector store."""
        return Mock()

    @pytest.fixture
    def mock_embedding_provider(self):
        """Create mock embedding provider."""
        return Mock()

    @pytest.fixture
    def service(self, mock_project_root, mock_vector_store, mock_embedding_provider):
        """Create MultiIndexQueryService instance."""
        return MultiIndexQueryService(
            project_root=mock_project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

    def test_query_returns_tuple_with_results_and_timing(
        self, service, mock_vector_store
    ):
        """Test that query() returns a tuple of (results, timing_dict)."""
        # Setup mock to return results and timing
        mock_vector_store.search.return_value = (
            [{"payload": {"path": "file1.py"}, "score": 0.9}],
            {"embedding_ms": 10, "hnsw_search_ms": 20},
        )

        # Execute query
        result = service.query(
            query_text="test query", limit=10, collection_name="voyage-code-3"
        )

        # Verify result is a tuple
        assert isinstance(result, tuple), "query() should return a tuple"
        assert len(result) == 2, "tuple should have 2 elements (results, timing)"

        results, timing = result
        assert isinstance(results, list), "First element should be results list"
        assert isinstance(timing, dict), "Second element should be timing dict"

    def test_single_index_timing_structure(self, service, mock_vector_store):
        """Test timing dict structure for single-index (code only) query."""
        # Setup mock - no multimodal index exists
        mock_vector_store.search.return_value = (
            [{"payload": {"path": "file1.py"}, "score": 0.9}],
            {"embedding_ms": 23, "hnsw_search_ms": 15},
        )

        # Execute query
        results, timing = service.query(
            query_text="test query", limit=10, collection_name="voyage-code-3"
        )

        # Verify timing structure for single index
        assert "code_index_ms" in timing, "Should have code_index_ms"
        assert "has_multimodal" in timing, "Should have has_multimodal flag"
        assert timing["has_multimodal"] is False, "has_multimodal should be False"
        assert (
            "multimodal_index_ms" not in timing
        ), "Should not have multimodal_index_ms when no multimodal"
        assert (
            "parallel_multi_index_ms" not in timing
        ), "Should not have parallel_multi_index_ms for single index"
        assert (
            "merge_deduplicate_ms" not in timing
        ), "Should not have merge timing for single index"
        assert "code_timed_out" in timing, "Should have code_timed_out flag"
        assert (
            timing["code_timed_out"] is False
        ), "code_timed_out should be False on success"

    def test_multi_index_timing_structure(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test timing dict structure for multi-index (code + multimodal) query."""
        # Create multimodal collection directory to simulate its existence
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock to return results and timing for both indexes
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                return (
                    [{"payload": {"path": "file2.md"}, "score": 0.85}],
                    {"embedding_ms": 41, "hnsw_search_ms": 30},
                )
            else:
                return (
                    [{"payload": {"path": "file1.py"}, "score": 0.9}],
                    {"embedding_ms": 23, "hnsw_search_ms": 15},
                )

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            # Execute query
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify timing structure for multi-index
        assert (
            "parallel_multi_index_ms" in timing
        ), "Should have parallel_multi_index_ms"
        assert "code_index_ms" in timing, "Should have code_index_ms"
        assert "multimodal_index_ms" in timing, "Should have multimodal_index_ms"
        assert "merge_deduplicate_ms" in timing, "Should have merge_deduplicate_ms"
        assert "has_multimodal" in timing, "Should have has_multimodal flag"
        assert timing["has_multimodal"] is True, "has_multimodal should be True"
        assert "code_timed_out" in timing, "Should have code_timed_out flag"
        assert "multimodal_timed_out" in timing, "Should have multimodal_timed_out flag"
        assert (
            timing["code_timed_out"] is False
        ), "code_timed_out should be False on success"
        assert (
            timing["multimodal_timed_out"] is False
        ), "multimodal_timed_out should be False on success"

    def test_parallel_timing_is_max_not_sum(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test that parallel_multi_index_ms is max of both indexes, not sum."""
        import time as time_module

        # Create multimodal collection directory
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock with known timings: code=23ms, multimodal=41ms
        # Add sleep to simulate actual execution time for parallel timing measurement
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                # Simulate 41ms execution
                time_module.sleep(0.041)
                return (
                    [{"payload": {"path": "file2.md"}, "score": 0.85}],
                    {"embedding_ms": 41, "hnsw_search_ms": 30},
                )
            else:
                # Simulate 23ms execution
                time_module.sleep(0.023)
                return (
                    [{"payload": {"path": "file1.py"}, "score": 0.9}],
                    {"embedding_ms": 23, "hnsw_search_ms": 15},
                )

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify parallel timing reflects concurrent execution
        # The simulated execution times are: code=23ms sleep, multimodal=41ms sleep
        # Individual times should now be wall-clock elapsed time (the sleep duration)
        code_time = timing["code_index_ms"]
        multimodal_time = timing["multimodal_index_ms"]
        parallel_time = timing["parallel_multi_index_ms"]

        # Individual times should be wall-clock elapsed (approx sleep time)
        assert (
            code_time >= 20 and code_time < 40
        ), f"code_index_ms {code_time}ms should be ~23ms (sleep time)"
        assert (
            multimodal_time >= 38 and multimodal_time < 60
        ), f"multimodal_index_ms {multimodal_time}ms should be ~41ms (sleep time)"

        # Parallel time should be close to max individual time (~41ms), not sum
        # Expected: parallel_time ≈ max(code_time, multimodal_time) + small overhead
        assert (
            parallel_time >= 40
        ), f"Parallel time {parallel_time}ms should be >= 40ms (approx max simulated time)"
        assert (
            parallel_time < 70
        ), f"Parallel time {parallel_time}ms should be < 70ms (not sum)"
        # Key invariant: parallel time >= max(individual times)
        assert (
            parallel_time >= max(code_time, multimodal_time) - 5
        ), f"Parallel time {parallel_time}ms must be >= max individual time {max(code_time, multimodal_time)}ms"

    def test_merge_deduplicate_timing_captured(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test that merge and deduplication timing is captured."""
        # Create multimodal collection directory
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                return (
                    [
                        {
                            "payload": {"path": "file2.md", "chunk_offset": 0},
                            "score": 0.85,
                        }
                    ],
                    {"embedding_ms": 20, "hnsw_search_ms": 15},
                )
            else:
                return (
                    [
                        {
                            "payload": {"path": "file1.py", "chunk_offset": 0},
                            "score": 0.9,
                        }
                    ],
                    {"embedding_ms": 23, "hnsw_search_ms": 15},
                )

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify merge timing exists and is reasonable
        assert (
            "merge_deduplicate_ms" in timing
        ), "Should capture merge/deduplicate timing"
        assert (
            timing["merge_deduplicate_ms"] >= 0
        ), "Merge timing should be non-negative"
        # Merge should be relatively fast (<50ms typically)
        assert (
            timing["merge_deduplicate_ms"] < 100
        ), "Merge timing should be reasonable (<100ms)"

    def test_timeout_flags_set_on_code_timeout(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test that code_timed_out flag is set when code index query times out."""
        # Create multimodal collection directory
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock to raise TimeoutError for code index
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                return (
                    [{"payload": {"path": "file2.md"}, "score": 0.85}],
                    {"embedding_ms": 41, "hnsw_search_ms": 30},
                )
            else:
                from concurrent.futures import TimeoutError

                raise TimeoutError("Code query timed out")

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify timeout flag is set for code index
        assert (
            timing["code_timed_out"] is True
        ), "code_timed_out should be True when code query times out"
        assert (
            timing["multimodal_timed_out"] is False
        ), "multimodal_timed_out should be False (multimodal succeeded)"
        # Should still return results from multimodal index
        assert len(results) > 0, "Should return partial results from multimodal index"

    def test_timeout_flags_set_on_multimodal_timeout(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test that multimodal_timed_out flag is set when multimodal index query times out."""
        # Create multimodal collection directory
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock to raise TimeoutError for multimodal
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                from concurrent.futures import TimeoutError

                raise TimeoutError("Multimodal query timed out")
            else:
                return (
                    [{"payload": {"path": "file1.py"}, "score": 0.9}],
                    {"embedding_ms": 23, "hnsw_search_ms": 15},
                )

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify timeout flag is set for multimodal index
        assert (
            timing["code_timed_out"] is False
        ), "code_timed_out should be False (code succeeded)"
        assert (
            timing["multimodal_timed_out"] is True
        ), "multimodal_timed_out should be True when multimodal query times out"
        # Should still return results from code index
        assert len(results) > 0, "Should return partial results from code index"

    def test_individual_index_timing_captured(
        self, service, mock_vector_store, mock_project_root
    ):
        """Test that individual index query times are captured as wall-clock elapsed time."""
        # Create multimodal collection directory
        multimodal_dir = (
            mock_project_root / ".code-indexer" / "index" / "voyage-multimodal-3"
        )
        multimodal_dir.mkdir(parents=True)

        # Setup mock - returns internal timing breakdown, but service measures wall-clock
        def search_side_effect(*args, **kwargs):
            collection = kwargs.get("collection_name", "")
            if collection == "voyage-multimodal-3":
                return (
                    [{"payload": {"path": "file2.md"}, "score": 0.85}],
                    {"embedding_ms": 41, "hnsw_search_ms": 30},
                )
            else:
                return (
                    [{"payload": {"path": "file1.py"}, "score": 0.9}],
                    {"embedding_ms": 23, "hnsw_search_ms": 15},
                )

        mock_vector_store.search.side_effect = search_side_effect

        # Mock multimodal provider
        with patch.object(service, "_get_multimodal_provider"):
            results, timing = service.query(
                query_text="test query", limit=10, collection_name="voyage-code-3"
            )

        # Verify timing values are present and non-negative (wall-clock elapsed time)
        # Actual values depend on execution time, not internal breakdown sums
        assert "code_index_ms" in timing, "Should have code_index_ms"
        assert "multimodal_index_ms" in timing, "Should have multimodal_index_ms"
        assert timing["code_index_ms"] >= 0, "code_index_ms should be non-negative"
        assert (
            timing["multimodal_index_ms"] >= 0
        ), "multimodal_index_ms should be non-negative"
        # Both should be numeric (wall-clock measurements)
        assert isinstance(
            timing["code_index_ms"], (int, float)
        ), "code_index_ms should be numeric"
        assert isinstance(
            timing["multimodal_index_ms"], (int, float)
        ), "multimodal_index_ms should be numeric"
