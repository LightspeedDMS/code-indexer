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

    def test_get_multimodal_provider_creates_cohere_client_when_cohere_collection_exists(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider creates CohereMultimodalClient when embed-v4.0-multimodal dir exists."""
        cohere_dir = project_root / ".code-indexer" / "index" / COHERE_MULTIMODAL_MODEL
        cohere_dir.mkdir(parents=True)

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

            provider = service._get_multimodal_provider()

            assert mock_cohere_cls.called, (
                "CohereMultimodalClient should be instantiated"
            )
            assert provider is mock_cohere_instance

    def test_get_multimodal_provider_falls_back_to_voyage_when_no_cohere_collection(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider falls back to VoyageMultimodalClient when no Cohere collection."""
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

            provider = service._get_multimodal_provider()

            assert mock_voyage_cls.called, (
                "VoyageMultimodalClient should be instantiated as fallback"
            )
            assert provider is mock_voyage_instance

    def test_get_multimodal_provider_is_cached_after_first_call(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider returns same instance on subsequent calls (lazy init)."""
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

            provider1 = service._get_multimodal_provider()
            provider2 = service._get_multimodal_provider()

            assert provider1 is provider2
            assert mock_voyage_cls.call_count == 1, "Should only instantiate once"

    def test_get_multimodal_provider_prefers_cohere_over_voyage_when_both_exist(
        self, project_root, mock_vector_store, mock_embedding_provider
    ):
        """_get_multimodal_provider creates CohereMultimodalClient when both collections exist."""
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
            mock_cohere_cls.return_value = Mock()
            mock_voyage_cls.return_value = Mock()

            service._get_multimodal_provider()

            assert mock_cohere_cls.called, "Cohere should take precedence"
            assert not mock_voyage_cls.called, (
                "VoyageAI should not be used when Cohere collection exists"
            )
