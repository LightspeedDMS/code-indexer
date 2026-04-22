"""Tests for Phase C (Story #883) Component 1 — precomputed-vector bypass.

Declared test list (exactly 3):
  1. test_precomputed_vector_bypasses_embedding_api_call
  2. test_precomputed_vector_passed_to_store_search
  3. test_without_precomputed_vector_passes_real_embedding_service_to_store

TDD: written BEFORE the implementation.

Design under test:
  SemanticSearchService._perform_semantic_search gains an optional
  `precomputed_query_vector: Optional[List[float]] = None` parameter.
  When provided and the backend is FilesystemVectorStore, the method must:
  - pass a _PrecomputedEmbeddingProvider (wrapping that vector) instead of
    the real embedding service
  - NOT call embedding_service.get_embedding()

When NOT provided, the real embedding_service is passed unchanged to store.search().

External dependencies mocked:
  - EmbeddingProviderFactory.create  (controls the embedding_service object)
  - BackendFactory.create            (controls the backend/store object)
  - ConfigManager.create_with_backtrack
  - FilesystemVectorStore.search
"""

from unittest.mock import MagicMock, patch
import pytest


def _make_mock_config() -> MagicMock:
    """Build a minimal mock config object accepted by _perform_semantic_search."""
    cfg = MagicMock()
    cfg.embedding_provider = "voyage-ai"
    return cfg


def _stub_filesystem_store(search_return=None):
    """Return a MagicMock that looks like a FilesystemVectorStore instance."""
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    store = MagicMock(spec=FilesystemVectorStore)
    store.search.return_value = (search_return or [], {})
    store.resolve_collection_name.return_value = "test_collection"
    return store


def _stub_backend(store):
    """Return a mock backend whose get_vector_store_client() returns *store*."""
    backend = MagicMock()
    backend.get_vector_store_client.return_value = store
    return backend


class TestPrecomputedVectorBypass:
    """Verify that _perform_semantic_search skips the embedding API when a
    precomputed vector is supplied (Story #883 Component 1)."""

    @pytest.fixture(autouse=True)
    def _patch_config_manager(self, tmp_path):
        """Patch ConfigManager so no real filesystem config is needed."""
        mock_cfg = _make_mock_config()
        with patch(
            "code_indexer.server.services.search_service.ConfigManager"
            ".create_with_backtrack"
        ) as mock_cm_cls:
            mock_cm = MagicMock()
            mock_cm.get_config.return_value = mock_cfg
            mock_cm_cls.return_value = mock_cm
            yield

    def test_precomputed_vector_bypasses_embedding_api_call(self, tmp_path):
        """When precomputed_query_vector is supplied, get_embedding must NOT be called.

        The whole point of precomputed-vector reuse is to avoid a second Voyage
        API round-trip.  If get_embedding is called, the optimisation is broken.
        """
        store = _stub_filesystem_store()
        backend = _stub_backend(store)
        embedding_service = MagicMock()
        embedding_service.get_embedding.return_value = [0.0] * 8  # must NOT be called

        precomputed = [0.1, 0.2, 0.3, 0.4]

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch(
                "code_indexer.server.app._server_hnsw_cache",
                None,
            ),
        ):
            from code_indexer.server.services.search_service import (
                SemanticSearchService,
            )

            svc = SemanticSearchService()
            svc._perform_semantic_search(
                repo_path=str(tmp_path),
                query="auth logic",
                limit=5,
                include_source=False,
                precomputed_query_vector=precomputed,
            )

        embedding_service.get_embedding.assert_not_called()

    def test_precomputed_vector_passed_to_store_search(self, tmp_path):
        """The precomputed vector must reach store.search() as the embedding provider's value.

        We verify this by capturing what store.search() received as
        `embedding_provider` and calling get_embedding on it — the result
        must equal the precomputed vector.
        """
        precomputed = [0.5, 0.6, 0.7, 0.8]
        captured_provider = {}

        def capture_search(**kwargs):
            captured_provider["provider"] = kwargs.get("embedding_provider")
            return [], {}

        store = _stub_filesystem_store()
        store.search.side_effect = capture_search
        backend = _stub_backend(store)
        embedding_service = MagicMock()

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch(
                "code_indexer.server.app._server_hnsw_cache",
                None,
            ),
        ):
            from code_indexer.server.services.search_service import (
                SemanticSearchService,
            )

            svc = SemanticSearchService()
            svc._perform_semantic_search(
                repo_path=str(tmp_path),
                query="auth logic",
                limit=5,
                include_source=False,
                precomputed_query_vector=precomputed,
            )

        provider = captured_provider.get("provider")
        assert provider is not None, "store.search() must receive an embedding_provider"
        returned_vector = provider.get_embedding(
            "ignored-text", embedding_purpose="query"
        )
        assert returned_vector == precomputed

    def test_without_precomputed_vector_passes_real_embedding_service_to_store(
        self, tmp_path
    ):
        """Regression: without a precomputed vector, the real embedding_service is passed
        to store.search() unchanged — the bypass is NOT active.

        This asserts backward compatibility: existing callers that do not provide
        precomputed_query_vector must continue to use the real embedding service.
        """
        store = _stub_filesystem_store()
        backend = _stub_backend(store)
        embedding_service = MagicMock()

        with (
            patch(
                "code_indexer.server.services.search_service.BackendFactory.create",
                return_value=backend,
            ),
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
                return_value=embedding_service,
            ),
            patch(
                "code_indexer.server.app._server_hnsw_cache",
                None,
            ),
        ):
            from code_indexer.server.services.search_service import (
                SemanticSearchService,
            )

            svc = SemanticSearchService()
            svc._perform_semantic_search(
                repo_path=str(tmp_path),
                query="auth logic",
                limit=5,
                include_source=False,
                # no precomputed_query_vector — normal path
            )

        store.search.assert_called_once()
        call_kwargs = store.search.call_args.kwargs
        # The real embedding_service (not a precomputed wrapper) must be wired in
        assert call_kwargs["embedding_provider"] is embedding_service, (
            "Without precomputed_query_vector, store.search() must receive "
            "the real embedding_service, not a precomputed wrapper"
        )
