"""
Tests for Bug #597: IndexHealthChecker hardcodes VoyageAI 1024 dimension assumption.

The bug: IndexHealthChecker.__init__ always reads from config.voyage_ai.embedding_dimensions
which does not exist on VoyageAIConfig, so getattr always returns the default 1024.
This causes false dimension violations when using Cohere embed-v4.0 (1536 dims) or
VoyageAI voyage-3-large (1536 dims).

The fix: resolve expected_dimensions from the active embedding provider:
- For voyage-ai: look up dimensions from the model name
- For cohere: use config.cohere.default_dimension
"""

from unittest.mock import MagicMock


def _make_mock_config(
    embedding_provider: str = "voyage-ai",
    voyage_model: str = "voyage-code-3",
    cohere_default_dimension: int = 1536,
) -> MagicMock:
    """Create a mock Config object for testing dimension resolution."""
    config = MagicMock()
    config.embedding_provider = embedding_provider

    # VoyageAI config — intentionally has NO embedding_dimensions attribute
    voyage_ai = MagicMock(spec=["model"])
    voyage_ai.model = voyage_model
    config.voyage_ai = voyage_ai

    # Cohere config — has default_dimension
    cohere = MagicMock()
    cohere.default_dimension = cohere_default_dimension
    config.cohere = cohere

    return config


def _make_mock_vector_store(collection_name: str = "voyage-3") -> MagicMock:
    """Create a minimal mock FilesystemVectorStore."""
    store = MagicMock()
    store.list_collections.return_value = [collection_name]
    return store


class TestIndexHealthCheckerDimensionResolution:
    """Bug #597: expected_dimensions must reflect the active provider."""

    def test_voyage_ai_code3_model_uses_1024(self):
        """voyage-code-3 has 1024 dimensions — checker must use 1024."""
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-code-3"
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.expected_dimensions == 1024, (
            f"Expected 1024 for voyage-code-3, got {checker.expected_dimensions}"
        )

    def test_voyage_ai_large2_model_uses_1536(self):
        """voyage-large-2 has 1536 dimensions — checker must use 1536, not 1024."""
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-large-2"
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.expected_dimensions == 1536, (
            f"Expected 1536 for voyage-large-2, got {checker.expected_dimensions}"
        )

    def test_voyage_ai_voyage3_large_model_uses_1536(self):
        """voyage-3-large has 1536 dimensions — checker must use 1536, not 1024."""
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-3-large"
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.expected_dimensions == 1536, (
            f"Expected 1536 for voyage-3-large, got {checker.expected_dimensions}"
        )

    def test_voyage_ai_unknown_model_raises_value_error(self):
        """
        Unknown VoyageAI model must raise ValueError — fail fast rather than
        silently producing wrong dimension checks (anti-fallback principle).
        """
        import pytest

        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-unknown-999"
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        with pytest.raises(ValueError, match="unknown.*model|voyage-unknown-999"):
            IndexHealthChecker(config, store)

    def test_cohere_provider_uses_cohere_default_dimension(self):
        """Cohere embed-v4.0 uses 1536 dimensions from CohereConfig.default_dimension."""
        config = _make_mock_config(
            embedding_provider="cohere",
            cohere_default_dimension=1536,
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.expected_dimensions == 1536, (
            f"Expected 1536 for cohere, got {checker.expected_dimensions}"
        )

    def test_cohere_provider_with_custom_dimension(self):
        """Cohere with custom dimension must use that dimension, not the VoyageAI hardcode."""
        config = _make_mock_config(
            embedding_provider="cohere",
            cohere_default_dimension=768,
        )
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.expected_dimensions == 768, (
            f"Expected 768 for cohere custom dimension, got {checker.expected_dimensions}"
        )

    def test_dimension_used_in_check_embedding_dimensions(self):
        """Dimension violations must be computed against the active provider dimension."""
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-large-2"
        )
        store = _make_mock_vector_store()

        # Simulate embeddings with 1536 dimensions (correct for voyage-large-2)
        store.sample_vectors.return_value = [
            {"vector": [0.1] * 1536, "payload": {"file_path": "a.py"}, "id": "1"},
            {"vector": [0.2] * 1536, "payload": {"file_path": "b.py"}, "id": "2"},
        ]

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)
        result = checker.check_embedding_dimensions()

        # With voyage-large-2 (1536 dims), 1536-dim vectors should have NO violations
        assert result.dimension_violations == [], (
            f"Expected no violations for 1536-dim vectors with voyage-large-2, "
            f"got {result.dimension_violations}"
        )
        assert result.dimension_consistency_score == 1.0

    def test_unknown_provider_raises_value_error(self):
        """
        Unknown embedding_provider must raise ValueError — fail fast rather than
        silently using wrong dimensions (anti-fallback principle).
        """
        import pytest

        config = _make_mock_config(embedding_provider="unknown-provider")
        store = _make_mock_vector_store()

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        with pytest.raises(ValueError, match="unknown-provider"):
            IndexHealthChecker(config, store)

    def test_old_bug_repro_voyage_large2_false_violations(self):
        """
        Regression: The old bug returned 1024 for voyage-large-2, causing 1536-dim
        vectors to appear as violations. This test confirms the bug is fixed.
        """
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-large-2"
        )
        store = _make_mock_vector_store()

        # 1536-dim embeddings — correct for voyage-large-2
        store.sample_vectors.return_value = [
            {"vector": [0.1] * 1536, "payload": {"file_path": "x.py"}, "id": "1"},
        ]

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        # Before fix: expected_dimensions was 1024, so 1536-dim vector → violation
        # After fix: expected_dimensions is 1536, so 1536-dim vector → no violation
        assert checker.expected_dimensions == 1536, "Bug still present: hardcoded 1024"

        result = checker.check_embedding_dimensions()
        assert len(result.dimension_violations) == 0, (
            "Bug still present: 1536-dim vector falsely reported as violation when "
            f"expected_dimensions={checker.expected_dimensions}"
        )


class TestHealthCheckerCollectionFallbackBug601:
    """Bug #601: fallback collection name must be 'code-index', not a provider-specific name."""

    def test_fallback_collection_name_is_code_index(self):
        """When list_collections() returns empty, collection_name must be 'code-index'."""
        config = _make_mock_config(
            embedding_provider="voyage-ai", voyage_model="voyage-code-3"
        )
        store = MagicMock()
        store.list_collections.return_value = []

        from code_indexer.server.validation.health_checker import IndexHealthChecker

        checker = IndexHealthChecker(config, store)

        assert checker.collection_name == "code-index", (
            f"Expected 'code-index' fallback, got '{checker.collection_name}'. "
            "Bug #601: fallback must not use a provider-specific name."
        )
