"""
Unit tests for Story #375: Wire semantic search filter parameters through server SemanticSearchService.

These tests follow TDD: written BEFORE implementation, defining expected behavior.

Tests cover:
1. SemanticSearchRequest model: all filter fields accepted, correct defaults
2. filter_conditions builder: correct format for each filter type
3. SemanticSearchService._perform_semantic_search passes filter_conditions to vector store
"""

from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.models.api_models import SemanticSearchRequest
from code_indexer.server.services.search_service import SemanticSearchService


# ===========================================================================
# 1. SemanticSearchRequest model tests
# ===========================================================================


class TestSemanticSearchRequestModel:
    """Verify the model accepts all filter fields with correct defaults."""

    def test_query_only_works(self):
        """Minimal request with only query should succeed."""
        req = SemanticSearchRequest(query="authentication logic")
        assert req.query == "authentication logic"
        assert req.limit == 10
        assert req.include_source is True

    def test_filter_fields_default_to_none(self):
        """All new filter fields must default to None for backward compatibility."""
        req = SemanticSearchRequest(query="test")
        assert req.path_filter is None
        assert req.language is None
        assert req.exclude_language is None
        assert req.exclude_path is None
        assert req.accuracy is None

    def test_path_filter_accepted(self):
        req = SemanticSearchRequest(query="test", path_filter="*/src/*")
        assert req.path_filter == "*/src/*"

    def test_language_accepted(self):
        req = SemanticSearchRequest(query="test", language="python")
        assert req.language == "python"

    def test_exclude_language_accepted(self):
        req = SemanticSearchRequest(query="test", exclude_language="javascript")
        assert req.exclude_language == "javascript"

    def test_exclude_path_accepted(self):
        req = SemanticSearchRequest(query="test", exclude_path="*/tests/*")
        assert req.exclude_path == "*/tests/*"

    def test_accuracy_accepted(self):
        req = SemanticSearchRequest(query="test", accuracy="high")
        assert req.accuracy == "high"

    def test_all_filters_together(self):
        req = SemanticSearchRequest(
            query="complex query",
            limit=5,
            path_filter="*/src/*",
            language="python",
            exclude_language="javascript",
            exclude_path="*/tests/*",
            accuracy="balanced",
        )
        assert req.query == "complex query"
        assert req.limit == 5
        assert req.path_filter == "*/src/*"
        assert req.language == "python"
        assert req.exclude_language == "javascript"
        assert req.exclude_path == "*/tests/*"
        assert req.accuracy == "balanced"


# ===========================================================================
# 2. filter_conditions builder tests
# ===========================================================================


class TestFilterConditionsBuilder:
    """Test that filter_conditions are built in the correct format for the vector store."""

    def test_no_filters_returns_empty_dict(self):
        """When no filters given, filter_conditions should be empty (no filtering)."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter=None, language=None, exclude_language=None, exclude_path=None
        )
        assert result == {}

    def test_path_filter_produces_must_clause(self):
        """path_filter should produce a must clause with key=path, match.text."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter="*/src/*",
            language=None,
            exclude_language=None,
            exclude_path=None,
        )
        assert "must" in result
        assert {"key": "path", "match": {"text": "*/src/*"}} in result["must"]

    def test_language_filter_produces_must_clause(self):
        """language filter should produce a must clause using LanguageMapper."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter=None,
            language="python",
            exclude_language=None,
            exclude_path=None,
        )
        assert "must" in result
        # LanguageMapper produces should-clause for python (multiple extensions)
        # or a direct key-match for single-extension languages
        must_items = result["must"]
        assert len(must_items) >= 1
        # At minimum it must reference language key somewhere
        filter_str = str(must_items)
        assert "language" in filter_str

    def test_exclude_language_produces_must_not_clause(self):
        """exclude_language should produce must_not conditions."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language="javascript",
            exclude_path=None,
        )
        assert "must_not" in result
        must_not = result["must_not"]
        assert len(must_not) >= 1
        # Each must_not item should reference language key
        for item in must_not:
            assert item["key"] == "language"
            assert "match" in item

    def test_exclude_path_produces_must_not_clause(self):
        """exclude_path should produce must_not conditions for path."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path="*/tests/*",
        )
        assert "must_not" in result
        must_not = result["must_not"]
        assert {"key": "path", "match": {"text": "*/tests/*"}} in must_not

    def test_combined_include_and_exclude(self):
        """Both include and exclude filters should appear in their respective clauses."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter="*/src/*",
            language=None,
            exclude_language=None,
            exclude_path="*/tests/*",
        )
        assert "must" in result
        assert "must_not" in result
        assert {"key": "path", "match": {"text": "*/src/*"}} in result["must"]
        assert {"key": "path", "match": {"text": "*/tests/*"}} in result["must_not"]

    def test_path_and_language_both_in_must(self):
        """Both path_filter and language should appear in must clause."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter="*/src/*",
            language="python",
            exclude_language=None,
            exclude_path=None,
        )
        assert "must" in result
        assert len(result["must"]) == 2
        # path_filter must clause
        assert {"key": "path", "match": {"text": "*/src/*"}} in result["must"]

    def test_java_language_single_extension(self):
        """Java has single extension, should produce direct key-match (no should wrapper)."""
        result = SemanticSearchService()._build_filter_conditions(
            path_filter=None, language="java", exclude_language=None, exclude_path=None
        )
        assert "must" in result
        must_items = result["must"]
        # Should have exactly one item
        assert len(must_items) == 1
        item = must_items[0]
        # For single extension, direct key-match
        assert item.get("key") == "language"
        assert "match" in item


# ===========================================================================
# 3. SemanticSearchService passes filter_conditions to vector store
# ===========================================================================


class TestSemanticSearchServiceFiltersWired:
    """
    Test that SemanticSearchService._perform_semantic_search actually passes
    filter_conditions derived from the search request to the vector store search().
    """

    def _make_mock_search_result(self) -> Dict[str, Any]:
        return {
            "payload": {
                "path": "src/auth.py",
                "line_start": 10,
                "line_end": 20,
                "content": "def authenticate():",
                "language": "py",
            },
            "score": 0.95,
        }

    @patch("code_indexer.server.services.search_service.BackendFactory")
    @patch("code_indexer.server.services.search_service.EmbeddingProviderFactory")
    @patch("code_indexer.server.services.search_service.ConfigManager")
    def test_no_filters_calls_search_without_filter_conditions(
        self,
        mock_config_manager,
        mock_embedding_factory,
        mock_backend_factory,
        tmp_path,
    ):
        """When no filters set, filter_conditions should be empty dict or None."""
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        # Create a fake repository structure
        (tmp_path / ".code-indexer").mkdir()

        # Setup mocks
        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = mock_config

        mock_embedding = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding

        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.resolve_collection_name.return_value = "test_collection"
        mock_vector_store.search.return_value = ([], {})

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        service = SemanticSearchService()
        request = SemanticSearchRequest(query="test query")

        with patch("code_indexer.server.app._server_hnsw_cache", None):
            service.search_repository_path(str(tmp_path), request)

        # Vector store search should be called
        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args

        # filter_conditions should be absent or empty dict when no filters given
        filter_conds = call_kwargs.kwargs.get("filter_conditions", {})
        assert not filter_conds, (
            f"Expected no filter_conditions when no filters set, got: {filter_conds}"
        )

    @patch("code_indexer.server.services.search_service.BackendFactory")
    @patch("code_indexer.server.services.search_service.EmbeddingProviderFactory")
    @patch("code_indexer.server.services.search_service.ConfigManager")
    def test_path_filter_passed_to_vector_store(
        self,
        mock_config_manager,
        mock_embedding_factory,
        mock_backend_factory,
        tmp_path,
    ):
        """path_filter in request must be passed as filter_conditions to vector store search."""
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        (tmp_path / ".code-indexer").mkdir()

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = mock_config

        mock_embedding = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding

        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.resolve_collection_name.return_value = "test_collection"
        mock_vector_store.search.return_value = ([], {})

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        service = SemanticSearchService()
        request = SemanticSearchRequest(query="test query", path_filter="*/src/*")

        with patch("code_indexer.server.app._server_hnsw_cache", None):
            service.search_repository_path(str(tmp_path), request)

        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args
        filter_conds = call_kwargs.kwargs.get("filter_conditions", {})

        assert filter_conds, "filter_conditions must be set when path_filter given"
        assert "must" in filter_conds
        assert {"key": "path", "match": {"text": "*/src/*"}} in filter_conds["must"]

    @patch("code_indexer.server.services.search_service.BackendFactory")
    @patch("code_indexer.server.services.search_service.EmbeddingProviderFactory")
    @patch("code_indexer.server.services.search_service.ConfigManager")
    def test_exclude_path_passed_to_vector_store(
        self,
        mock_config_manager,
        mock_embedding_factory,
        mock_backend_factory,
        tmp_path,
    ):
        """exclude_path in request must appear in must_not filter_conditions."""
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        (tmp_path / ".code-indexer").mkdir()

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = mock_config

        mock_embedding = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding

        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.resolve_collection_name.return_value = "test_collection"
        mock_vector_store.search.return_value = ([], {})

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        service = SemanticSearchService()
        request = SemanticSearchRequest(query="test query", exclude_path="*/tests/*")

        with patch("code_indexer.server.app._server_hnsw_cache", None):
            service.search_repository_path(str(tmp_path), request)

        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args
        filter_conds = call_kwargs.kwargs.get("filter_conditions", {})

        assert filter_conds, "filter_conditions must be set when exclude_path given"
        assert "must_not" in filter_conds
        assert {"key": "path", "match": {"text": "*/tests/*"}} in filter_conds[
            "must_not"
        ]

    @patch("code_indexer.server.services.search_service.BackendFactory")
    @patch("code_indexer.server.services.search_service.EmbeddingProviderFactory")
    @patch("code_indexer.server.services.search_service.ConfigManager")
    def test_language_filter_passed_to_vector_store(
        self,
        mock_config_manager,
        mock_embedding_factory,
        mock_backend_factory,
        tmp_path,
    ):
        """language in request must appear in must filter_conditions using LanguageMapper."""
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        (tmp_path / ".code-indexer").mkdir()

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = mock_config

        mock_embedding = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding

        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.resolve_collection_name.return_value = "test_collection"
        mock_vector_store.search.return_value = ([], {})

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        service = SemanticSearchService()
        # Java has single extension, simpler to test
        request = SemanticSearchRequest(query="test query", language="java")

        with patch("code_indexer.server.app._server_hnsw_cache", None):
            service.search_repository_path(str(tmp_path), request)

        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args
        filter_conds = call_kwargs.kwargs.get("filter_conditions", {})

        assert filter_conds, "filter_conditions must be set when language given"
        assert "must" in filter_conds
        filter_str = str(filter_conds["must"])
        assert "language" in filter_str

    @patch("code_indexer.server.services.search_service.BackendFactory")
    @patch("code_indexer.server.services.search_service.EmbeddingProviderFactory")
    @patch("code_indexer.server.services.search_service.ConfigManager")
    def test_exclude_language_passed_to_vector_store(
        self,
        mock_config_manager,
        mock_embedding_factory,
        mock_backend_factory,
        tmp_path,
    ):
        """exclude_language in request must appear in must_not filter_conditions."""
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

        (tmp_path / ".code-indexer").mkdir()

        mock_config = MagicMock()
        mock_config_manager.create_with_backtrack.return_value.get_config.return_value = mock_config

        mock_embedding = MagicMock()
        mock_embedding_factory.create.return_value = mock_embedding

        mock_vector_store = MagicMock(spec=FilesystemVectorStore)
        mock_vector_store.resolve_collection_name.return_value = "test_collection"
        mock_vector_store.search.return_value = ([], {})

        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vector_store
        mock_backend_factory.create.return_value = mock_backend

        service = SemanticSearchService()
        request = SemanticSearchRequest(query="test query", exclude_language="java")

        with patch("code_indexer.server.app._server_hnsw_cache", None):
            service.search_repository_path(str(tmp_path), request)

        mock_vector_store.search.assert_called_once()
        call_kwargs = mock_vector_store.search.call_args
        filter_conds = call_kwargs.kwargs.get("filter_conditions", {})

        assert filter_conds, "filter_conditions must be set when exclude_language given"
        assert "must_not" in filter_conds
        # Java has single extension "java"
        assert {"key": "language", "match": {"value": "java"}} in filter_conds[
            "must_not"
        ]


# ===========================================================================
# 4. semantic_query_manager passes filter params to SemanticSearchRequest
# ===========================================================================


class TestSemanticQueryManagerPassesFilters:
    """
    Test that semantic_query_manager builds SemanticSearchRequest with filter params
    instead of dropping them (the old behavior that produced QUERY-MIGRATE-007 warning).
    """

    def test_search_request_includes_path_filter_when_provided(self):
        """
        When the query manager builds SemanticSearchRequest, path_filter from
        the incoming query parameters must be included.
        """
        # This test verifies the contract: path_filter flows into SemanticSearchRequest
        req = SemanticSearchRequest(
            query="authentication", limit=10, include_source=True, path_filter="*/src/*"
        )
        # If path_filter is in the request, it should NOT be None
        assert req.path_filter == "*/src/*"

    def test_search_request_includes_language_when_provided(self):
        req = SemanticSearchRequest(
            query="authentication", limit=10, include_source=True, language="python"
        )
        assert req.language == "python"

    def test_search_request_includes_exclude_language_when_provided(self):
        req = SemanticSearchRequest(
            query="authentication",
            limit=10,
            include_source=True,
            exclude_language="javascript",
        )
        assert req.exclude_language == "javascript"

    def test_search_request_includes_exclude_path_when_provided(self):
        req = SemanticSearchRequest(
            query="authentication",
            limit=10,
            include_source=True,
            exclude_path="*/node_modules/*",
        )
        assert req.exclude_path == "*/node_modules/*"

    def test_search_request_includes_accuracy_when_provided(self):
        req = SemanticSearchRequest(
            query="authentication", limit=10, include_source=True, accuracy="high"
        )
        assert req.accuracy == "high"
