"""
Unit tests for SemanticSearchService API metrics - NO double-counting.

Tests that SemanticSearchService.search_repository_path() does NOT track API metrics,
because metrics tracking is already done at the MCP/REST entry point layer
(semantic_query_manager._perform_search()).

Story: Fix Double-Counting Bug in API Metrics
Root Cause: Metrics were tracked in both:
  1. semantic_query_manager._perform_search() at line 728 (CORRECT - keep this)
  2. search_service.search_repository_path() at line 112 (INCORRECT - removed)

This test ensures the duplicate tracking in search_service.py is removed.
"""

import pytest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.code_indexer.server.services.search_service import SemanticSearchService
from src.code_indexer.server.models.api_models import SemanticSearchRequest


class TestSearchServiceNoDoubleMetrics:
    """Test SemanticSearchService does NOT track metrics (to prevent double-counting)."""

    @pytest.fixture
    def test_repo_with_filesystem_backend(self):
        """Create test repository with filesystem backend configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir) / "test_repo"
            repo_path.mkdir()

            # Create .code-indexer directory
            config_dir = repo_path / ".code-indexer"
            config_dir.mkdir()

            # Create config.json with filesystem backend
            config_data = {
                "embedding": {
                    "provider": "voyage",
                    "model": "voyage-3-large",
                    "dimensions": 1024,
                },
                "vector_store": {"provider": "filesystem"},
                "chunking": {
                    "chunk_size": 512,
                    "chunk_overlap": 128,
                    "tree_sitter_config": {"python": {"enabled": True}},
                },
            }
            config_file = config_dir / "config.json"
            config_file.write_text(json.dumps(config_data, indent=2))

            # Create index directory (required for FilesystemVectorStore)
            index_dir = config_dir / "index"
            index_dir.mkdir()

            yield str(repo_path)

    def test_search_repository_path_does_not_call_increment_semantic_search(
        self, test_repo_with_filesystem_backend
    ):
        """
        CRITICAL TEST: Verify search_repository_path() does NOT call api_metrics_service.

        The metrics tracking should ONLY happen in semantic_query_manager._perform_search(),
        which is the single entry point for ALL MCP/REST search operations.

        If search_service also tracks metrics, we get double-counting because:
        - semantic_query_manager._perform_search() calls increment_semantic_search()
        - then it calls search_repository_path() which would call it again

        This test MUST FAIL before the fix and PASS after removing the duplicate
        metrics tracking from search_service.py lines 108-112.
        """
        repo_path = test_repo_with_filesystem_backend
        search_service = SemanticSearchService()

        # Create a fresh mock for api_metrics_service to track calls
        mock_api_metrics = MagicMock()

        # Mock the entire search flow to avoid needing real embeddings/indexes
        mock_embedding_service = MagicMock()
        mock_embedding_service.get_embedding.return_value = [0.1] * 1024

        mock_vector_store = MagicMock()
        mock_vector_store.search.return_value = ([], {})
        mock_vector_store.resolve_collection_name.return_value = "test_collection"

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store

        # Patch all external dependencies to isolate the test
        # Note: api_metrics_service is imported locally in search_repository_path(),
        # so we patch the actual module where it's defined
        with patch(
            "src.code_indexer.server.services.api_metrics_service.api_metrics_service",
            mock_api_metrics,
        ):
            with patch(
                "src.code_indexer.server.services.search_service.ConfigManager.create_with_backtrack"
            ) as mock_config_manager:
                mock_config = MagicMock()
                mock_config_manager.return_value.get_config.return_value = mock_config

                with patch(
                    "src.code_indexer.server.services.search_service.BackendFactory.create",
                    return_value=mock_backend,
                ):
                    with patch(
                        "src.code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                        return_value=mock_embedding_service,
                    ):
                        search_request = SemanticSearchRequest(
                            query="test query", limit=5, include_source=True
                        )

                        # Execute the search - with all mocks in place, this should succeed
                        search_service.search_repository_path(repo_path, search_request)

        # CRITICAL ASSERTION: api_metrics_service should NOT be called
        # Metrics tracking is done at the MCP/REST entry point level
        # (semantic_query_manager._perform_search), not here
        assert mock_api_metrics.increment_semantic_search.call_count == 0, (
            "search_repository_path() should NOT call api_metrics_service.increment_semantic_search()! "
            "Metrics tracking is done at the MCP entry point (semantic_query_manager._perform_search). "
            "Having it here causes double-counting."
        )

    def test_search_repository_does_not_call_increment_semantic_search(
        self, test_repo_with_filesystem_backend
    ):
        """
        Verify search_repository() (the wrapper method) also does NOT call api_metrics_service.

        search_repository() delegates to search_repository_path(), so if we remove metrics
        from search_repository_path(), this should also not track metrics.
        """
        repo_path = test_repo_with_filesystem_backend
        search_service = SemanticSearchService()

        mock_api_metrics = MagicMock()

        # Mock _get_repository_path to return our test repo path
        with patch.object(
            search_service, "_get_repository_path", return_value=repo_path
        ):
            # Mock search_repository_path to avoid the full search flow
            with patch.object(
                search_service, "search_repository_path"
            ) as mock_search_path:
                mock_search_path.return_value = MagicMock()

                with patch(
                    "src.code_indexer.server.services.api_metrics_service.api_metrics_service",
                    mock_api_metrics,
                ):
                    search_request = SemanticSearchRequest(
                        query="test query", limit=5, include_source=True
                    )

                    search_service.search_repository("test-repo", search_request)

        # Verify no metrics calls from search_repository() itself
        assert mock_api_metrics.increment_semantic_search.call_count == 0, (
            "search_repository() should NOT call api_metrics_service.increment_semantic_search()!"
        )
