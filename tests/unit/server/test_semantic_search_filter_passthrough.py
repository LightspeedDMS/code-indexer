"""
Unit tests for Story #375: SemanticSearchService filter parameter passthrough.

Tests cover:
- AC1: path_filter passed from search_repository_path -> _perform_semantic_search -> vector store
- AC2: language and exclude_language passed through
- AC3: exclude_path passed through
- AC4: accuracy passed through
- Backward compat: no filters still works

TDD: written before implementation to define expected behavior.
Production file: src/code_indexer/server/services/search_service.py
"""

import pytest
from unittest.mock import MagicMock, patch

from src.code_indexer.server.models.api_models import (
    SemanticSearchRequest,
    SemanticSearchResponse,
)


class TestSearchRepositoryPathFilterPassthrough:
    """
    Tests that search_repository_path() passes all filter params to _perform_semantic_search().
    """

    def test_passes_path_filter_to_perform_search(self, tmp_path):
        """AC1: path_filter is forwarded from search_repository_path to _perform_semantic_search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]) as mock_perform:
            request = SemanticSearchRequest(
                query="test",
                limit=10,
                path_filter="*/src/*",
            )
            service.search_repository_path(repo_path=str(tmp_path), search_request=request)

            call_kwargs = mock_perform.call_args.kwargs
            assert call_kwargs.get("path_filter") == "*/src/*"

    def test_passes_language_to_perform_search(self, tmp_path):
        """AC2: language is forwarded to _perform_semantic_search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]) as mock_perform:
            request = SemanticSearchRequest(
                query="test",
                limit=10,
                language="python",
            )
            service.search_repository_path(repo_path=str(tmp_path), search_request=request)

            call_kwargs = mock_perform.call_args.kwargs
            assert call_kwargs.get("language") == "python"

    def test_passes_exclude_language_to_perform_search(self, tmp_path):
        """AC2: exclude_language is forwarded to _perform_semantic_search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]) as mock_perform:
            request = SemanticSearchRequest(
                query="test",
                limit=10,
                exclude_language="javascript",
            )
            service.search_repository_path(repo_path=str(tmp_path), search_request=request)

            call_kwargs = mock_perform.call_args.kwargs
            assert call_kwargs.get("exclude_language") == "javascript"

    def test_passes_exclude_path_to_perform_search(self, tmp_path):
        """AC3: exclude_path is forwarded to _perform_semantic_search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]) as mock_perform:
            request = SemanticSearchRequest(
                query="test",
                limit=10,
                exclude_path="*/tests/*",
            )
            service.search_repository_path(repo_path=str(tmp_path), search_request=request)

            call_kwargs = mock_perform.call_args.kwargs
            assert call_kwargs.get("exclude_path") == "*/tests/*"

    def test_passes_accuracy_to_perform_search(self, tmp_path):
        """AC4: accuracy is forwarded to _perform_semantic_search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]) as mock_perform:
            request = SemanticSearchRequest(
                query="test",
                limit=10,
                accuracy="high",
            )
            service.search_repository_path(repo_path=str(tmp_path), search_request=request)

            call_kwargs = mock_perform.call_args.kwargs
            assert call_kwargs.get("accuracy") == "high"

    def test_no_filters_produces_valid_response(self, tmp_path):
        """Backward compat: no filter fields still returns valid SemanticSearchResponse."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        with patch.object(service, "_perform_semantic_search", return_value=[]):
            request = SemanticSearchRequest(query="test", limit=10)
            result = service.search_repository_path(
                repo_path=str(tmp_path), search_request=request
            )

            assert isinstance(result, SemanticSearchResponse)
            assert result.query == "test"
            assert result.results == []
            assert result.total == 0


class TestPerformSemanticSearchFilterConditions:
    """
    Tests that _perform_semantic_search() builds and passes filter_conditions
    to FilesystemVectorStore.search() matching the CLI reference pattern.
    """

    def _make_search_service_with_mocks(self, mock_vector_store, tmp_path):
        """Helper to set up SemanticSearchService with mocked backend."""
        from src.code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()
        return service

    @patch("src.code_indexer.server.services.search_service.ConfigManager")
    @patch("src.code_indexer.server.services.search_service.BackendFactory")
    @patch("src.code_indexer.server.services.search_service.EmbeddingProviderFactory")
    def test_path_filter_becomes_filter_condition_for_vector_store(
        self, mock_emb_factory, mock_backend_factory, mock_config_manager, tmp_path
    ):
        """AC1: path_filter produces filter_conditions passed to vector store search."""
        from src.code_indexer.server.services.search_service import SemanticSearchService
        from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = (
            mock_config
        )
        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.search.return_value = ([], {})
        mock_vector_store.resolve_collection_name.return_value = "test_col"
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend
        mock_emb_factory.create.return_value = MagicMock()

        service = SemanticSearchService()

        with patch("src.code_indexer.server.app._server_hnsw_cache", None):
            service._perform_semantic_search(
                repo_path=str(tmp_path),
                query="test",
                limit=10,
                include_source=True,
                path_filter="*/src/*",
            )

        call_kwargs = mock_vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs.get("filter_conditions")

        assert filter_conditions is not None
        # Must contain a path condition
        if isinstance(filter_conditions, list):
            keys = [c.get("key") for c in filter_conditions]
            assert "path" in keys
        elif isinstance(filter_conditions, dict):
            must = filter_conditions.get("must", [])
            keys = [c.get("key") for c in must]
            assert "path" in keys

    @patch("src.code_indexer.server.services.search_service.ConfigManager")
    @patch("src.code_indexer.server.services.search_service.BackendFactory")
    @patch("src.code_indexer.server.services.search_service.EmbeddingProviderFactory")
    def test_language_filter_becomes_filter_condition_for_vector_store(
        self, mock_emb_factory, mock_backend_factory, mock_config_manager, tmp_path
    ):
        """AC2: language filter produces filter_conditions passed to vector store."""
        from src.code_indexer.server.services.search_service import SemanticSearchService
        from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = (
            mock_config
        )
        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.search.return_value = ([], {})
        mock_vector_store.resolve_collection_name.return_value = "test_col"
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend
        mock_emb_factory.create.return_value = MagicMock()

        service = SemanticSearchService()

        with patch("src.code_indexer.server.app._server_hnsw_cache", None):
            service._perform_semantic_search(
                repo_path=str(tmp_path),
                query="test",
                limit=10,
                include_source=True,
                language="python",
            )

        call_kwargs = mock_vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs.get("filter_conditions")

        assert filter_conditions is not None
        filter_str = str(filter_conditions)
        assert "language" in filter_str, f"Expected 'language' in filter_conditions: {filter_conditions}"

    @patch("src.code_indexer.server.services.search_service.ConfigManager")
    @patch("src.code_indexer.server.services.search_service.BackendFactory")
    @patch("src.code_indexer.server.services.search_service.EmbeddingProviderFactory")
    def test_no_filters_passes_none_or_empty_filter_conditions(
        self, mock_emb_factory, mock_backend_factory, mock_config_manager, tmp_path
    ):
        """Backward compat: no filters passes None or empty filter_conditions."""
        from src.code_indexer.server.services.search_service import SemanticSearchService
        from src.code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = (
            mock_config
        )
        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.search.return_value = ([], {})
        mock_vector_store.resolve_collection_name.return_value = "test_col"
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend
        mock_emb_factory.create.return_value = MagicMock()

        service = SemanticSearchService()

        with patch("src.code_indexer.server.app._server_hnsw_cache", None):
            service._perform_semantic_search(
                repo_path=str(tmp_path),
                query="test",
                limit=10,
                include_source=True,
                # No filter params
            )

        call_kwargs = mock_vector_store.search.call_args.kwargs
        filter_conditions = call_kwargs.get("filter_conditions")
        # Should be None or falsy when no filters specified
        assert not filter_conditions
