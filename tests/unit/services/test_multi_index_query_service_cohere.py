"""
Unit tests for MultiIndexQueryService Cohere multimodal support.

Tests that MultiIndexQueryService correctly detects and uses Cohere multimodal
collections (embed-v4.0-multimodal) alongside VoyageAI collections.
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.services.multi_index_query_service import (
    MultiIndexQueryService,
    MULTIMODAL_MODELS,
)
from code_indexer.config import COHERE_MULTIMODAL_MODEL, VOYAGE_MULTIMODAL_MODEL


@pytest.fixture
def mock_vector_store():
    """Create mock vector store client."""
    store = Mock()
    store.search = Mock(return_value=([], {}))
    return store


@pytest.fixture
def mock_embedding_provider():
    """Create mock embedding provider with sentinel embedding (dimension irrelevant)."""
    provider = Mock()
    provider.embed_query = Mock(return_value=[0.0])
    return provider


@pytest.fixture
def project_root(tmp_path):
    """Create temporary project root with .code-indexer/index structure."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    index_dir = project_dir / ".code-indexer" / "index"
    index_dir.mkdir(parents=True)
    return project_dir


class TestMultiIndexQueryServiceCohereDetection:
    """Tests for Cohere multimodal collection detection."""

    def test_multimodal_models_constant_contains_both_providers(self):
        """MULTIMODAL_MODELS list contains both VoyageAI and Cohere model names."""
        assert VOYAGE_MULTIMODAL_MODEL in MULTIMODAL_MODELS
        assert COHERE_MULTIMODAL_MODEL in MULTIMODAL_MODELS

    def test_has_multimodal_index_detects_cohere_collection(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """has_multimodal_index returns True when embed-v4.0-multimodal dir exists."""
        cohere_dir = project_root / ".code-indexer" / "index" / COHERE_MULTIMODAL_MODEL
        cohere_dir.mkdir(parents=True)

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        assert service.has_multimodal_index() is True

    def test_will_query_multimodal_returns_true_for_cohere_collection(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """will_query_multimodal returns True when Cohere collection exists (no voyage- guard)."""
        cohere_dir = project_root / ".code-indexer" / "index" / COHERE_MULTIMODAL_MODEL
        cohere_dir.mkdir(parents=True)

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        assert service.will_query_multimodal() is True

    def test_get_multimodal_provider_creates_cohere_client_for_cohere_model_name(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider(COHERE_MULTIMODAL_MODEL) creates CohereMultimodalClient.

        Bug #1483: provider selection is now keyed by the explicit model_name
        argument, never by which collection(s) happen to exist on disk — the
        caller (the per-collection query loop) supplies the model_name it is
        about to search, so there is no separate "detect on disk" decision
        that could disagree.
        """
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        # Patch at source module since the import is lazy (local import inside method)
        with patch(
            "code_indexer.services.cohere_multimodal.CohereMultimodalClient"
        ) as mock_cohere_cls:
            mock_cohere_instance = Mock()
            mock_cohere_cls.return_value = mock_cohere_instance

            provider = service._get_multimodal_provider(COHERE_MULTIMODAL_MODEL)

            assert mock_cohere_cls.called, (
                "CohereMultimodalClient should be instantiated"
            )
            assert provider is mock_cohere_instance

    def test_get_multimodal_provider_creates_voyage_client_for_voyage_model_name(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider(VOYAGE_MULTIMODAL_MODEL) creates VoyageMultimodalClient."""
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        # Patch at source module since the import is lazy (local import inside method)
        with patch(
            "code_indexer.services.voyage_multimodal.VoyageMultimodalClient"
        ) as mock_voyage_cls:
            mock_voyage_instance = Mock()
            mock_voyage_cls.return_value = mock_voyage_instance

            provider = service._get_multimodal_provider(VOYAGE_MULTIMODAL_MODEL)

            assert mock_voyage_cls.called, (
                "VoyageMultimodalClient should be instantiated"
            )
            assert provider is mock_voyage_instance

    def test_get_multimodal_provider_is_cached_per_model_after_first_call(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider(model_name) returns same instance on
        subsequent calls with the SAME model_name (lazy init, cached per key)."""
        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        # Patch at source module since the import is lazy (local import inside method)
        with patch(
            "code_indexer.services.voyage_multimodal.VoyageMultimodalClient"
        ) as mock_voyage_cls:
            mock_voyage_cls.return_value = Mock()

            provider1 = service._get_multimodal_provider(VOYAGE_MULTIMODAL_MODEL)
            provider2 = service._get_multimodal_provider(VOYAGE_MULTIMODAL_MODEL)

            assert provider1 is provider2
            assert mock_voyage_cls.call_count == 1, "Should only instantiate once"

    def test_get_multimodal_provider_resolves_independently_per_model_when_both_exist(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """Bug #1483 regression guard: when BOTH multimodal collections exist
        on disk, requesting the provider for EACH model_name must return the
        CORRECT, matching provider for that model — never a single shared
        "winner" that silently overrides the other's requests. This is the
        exact disagreement that used to cause a dimension mismatch (Cohere
        provider embedding a query later searched against the Voyage
        collection, or vice versa).
        """
        (project_root / ".code-indexer" / "index" / COHERE_MULTIMODAL_MODEL).mkdir(
            parents=True
        )
        (project_root / ".code-indexer" / "index" / VOYAGE_MULTIMODAL_MODEL).mkdir(
            parents=True
        )

        service = MultiIndexQueryService(
            project_root=project_root,
            vector_store=mock_vector_store,
            embedding_provider=mock_embedding_provider,
        )

        # Patch at source modules since imports are lazy (local imports inside method)
        with (
            patch(
                "code_indexer.services.cohere_multimodal.CohereMultimodalClient"
            ) as mock_cohere_cls,
            patch(
                "code_indexer.services.voyage_multimodal.VoyageMultimodalClient"
            ) as mock_voyage_cls,
        ):
            mock_cohere_instance = Mock()
            mock_voyage_instance = Mock()
            mock_cohere_cls.return_value = mock_cohere_instance
            mock_voyage_cls.return_value = mock_voyage_instance

            cohere_provider = service._get_multimodal_provider(COHERE_MULTIMODAL_MODEL)
            voyage_provider = service._get_multimodal_provider(VOYAGE_MULTIMODAL_MODEL)

            assert cohere_provider is mock_cohere_instance
            assert voyage_provider is mock_voyage_instance
            assert cohere_provider is not voyage_provider
            assert mock_cohere_cls.called
            assert mock_voyage_cls.called
