"""
Unit tests for Cohere model dimension entries in health_checker.py.

Tests that MODEL_DIMENSIONS contains the Cohere embedding models with the
correct expected dimension (DEFAULT_COHERE_DIM = 1536).
"""

from code_indexer.server.validation.health_checker import (
    MODEL_DIMENSIONS,
    DEFAULT_COHERE_DIM,
)


class TestHealthCheckerCohereModelDimensions:
    """Tests that MODEL_DIMENSIONS includes Cohere models."""

    def test_model_dimensions_contains_embed_v4(self):
        """MODEL_DIMENSIONS contains embed-v4.0 with DEFAULT_COHERE_DIM."""
        assert "embed-v4.0" in MODEL_DIMENSIONS
        assert MODEL_DIMENSIONS["embed-v4.0"] == DEFAULT_COHERE_DIM

    def test_model_dimensions_contains_embed_v4_multimodal(self):
        """MODEL_DIMENSIONS contains embed-v4.0-multimodal with DEFAULT_COHERE_DIM."""
        assert "embed-v4.0-multimodal" in MODEL_DIMENSIONS
        assert MODEL_DIMENSIONS["embed-v4.0-multimodal"] == DEFAULT_COHERE_DIM

    def test_default_cohere_dim_is_1536(self):
        """DEFAULT_COHERE_DIM is 1536 (Cohere embed-v4.0 native dimension)."""
        assert DEFAULT_COHERE_DIM == 1536
