"""Tests for Cohere embedding provider and multi-provider infrastructure.

Increment 1: ABC embedding_purpose kwarg and VoyageAI acceptance.
Increment 2: Cohere provider, factory, slug, config, and batch tests.
"""

import os
import pytest
from unittest.mock import patch


@pytest.fixture
def voyage_client():
    """Create a VoyageAIClient with mocked API key for testing."""
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import VoyageAIConfig

        config = VoyageAIConfig()
        yield VoyageAIClient(config, None)


@pytest.fixture
def cohere_provider():
    """Create a CohereEmbeddingProvider with mocked API key for testing."""
    with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        config = CohereConfig()
        yield CohereEmbeddingProvider(config, None)


class TestABCEmbeddingPurposeParam:
    """The ABC and VoyageAI must accept embedding_purpose as keyword-only arg."""

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_document(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='document' kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="document")
        assert isinstance(result, list)

    def test_voyage_ai_get_embedding_accepts_embedding_purpose_query(
        self, voyage_client
    ):
        """VoyageAI.get_embedding must accept embedding_purpose='query' kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding("hello", embedding_purpose="query")
        assert isinstance(result, list)

    def test_voyage_ai_get_embeddings_batch_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch must accept embedding_purpose kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {
                "data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]
            }
            result = voyage_client.get_embeddings_batch(
                ["hello", "world"], embedding_purpose="document"
            )
        assert len(result) == 2

    def test_voyage_ai_get_embedding_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embedding_with_metadata must accept embedding_purpose kwarg."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
            result = voyage_client.get_embedding_with_metadata(
                "hello", embedding_purpose="document"
            )
        assert result.embedding == [0.1, 0.2, 0.3]

    def test_voyage_ai_get_embeddings_batch_with_metadata_accepts_embedding_purpose(
        self, voyage_client
    ):
        """VoyageAI.get_embeddings_batch_with_metadata must accept embedding_purpose."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
            result = voyage_client.get_embeddings_batch_with_metadata(
                ["hello"], embedding_purpose="document"
            )
        assert len(result.embeddings) == 1

    def test_voyage_ai_health_check_accepts_test_api_as_keyword_arg(
        self, voyage_client
    ):
        """VoyageAI.health_check must accept test_api as keyword arg."""
        result = voyage_client.health_check(test_api=False)
        assert result is True

    def test_voyage_ai_embedding_purpose_does_not_affect_request_payload(
        self, voyage_client
    ):
        """VoyageAI ignores embedding_purpose; payload sent to API must be unchanged."""
        with patch.object(voyage_client, "_make_sync_request") as mock_req:
            mock_req.return_value = {"data": [{"embedding": [0.1]}]}
            voyage_client.get_embedding("test", embedding_purpose="query")
            call_args = mock_req.call_args
            positional_args = call_args[0]
            # First positional arg is texts list
            assert positional_args[0] == ["test"]


class TestCohereProviderInstantiation:
    """Cohere provider creation and basic property tests."""

    def test_cohere_provider_creation_with_api_key(self, cohere_provider):
        """CohereEmbeddingProvider must instantiate without error when API key is set."""
        assert cohere_provider is not None

    def test_cohere_provider_raises_without_api_key(self):
        """CohereEmbeddingProvider must raise ValueError when no API key available."""
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import CohereConfig

        with patch.dict(os.environ, {}, clear=True):
            # Ensure CO_API_KEY is not set and config has no key
            env_without_cohere = {
                k: v for k, v in os.environ.items() if k != "CO_API_KEY"
            }
            with patch.dict(os.environ, env_without_cohere, clear=True):
                config = CohereConfig(api_key="")
                with pytest.raises(ValueError, match="Cohere API key required"):
                    CohereEmbeddingProvider(config, None)

    def test_cohere_provider_name(self, cohere_provider):
        """get_provider_name() must return 'cohere'."""
        assert cohere_provider.get_provider_name() == "cohere"

    def test_cohere_current_model(self, cohere_provider):
        """get_current_model() must return 'embed-v4.0'."""
        assert cohere_provider.get_current_model() == "embed-v4.0"

    def test_cohere_supports_batch(self, cohere_provider):
        """supports_batch_processing() must return True."""
        assert cohere_provider.supports_batch_processing() is True

    def test_cohere_model_info(self, cohere_provider):
        """get_model_info() must return dict with correct keys."""
        info = cohere_provider.get_model_info()
        assert isinstance(info, dict)
        assert "name" in info
        assert "provider" in info
        assert "dimensions" in info
        assert "default_dimension" in info
        assert "max_tokens" in info
        assert "max_texts_per_request" in info
        assert "supports_batch" in info
        assert "api_endpoint" in info
        assert info["provider"] == "cohere"
        assert info["name"] == "embed-v4.0"
        assert info["supports_batch"] is True


class TestCohereEmbeddingPurposeMapping:
    """Cohere embedding_purpose to input_type mapping."""

    def test_map_document_to_search_document(self, cohere_provider):
        """_map_embedding_purpose('document') must return 'search_document'."""
        assert cohere_provider._map_embedding_purpose("document") == "search_document"

    def test_map_query_to_search_query(self, cohere_provider):
        """_map_embedding_purpose('query') must return 'search_query'."""
        assert cohere_provider._map_embedding_purpose("query") == "search_query"


class TestFactoryProviderCreation:
    """EmbeddingProviderFactory.create() and provider discovery."""

    def test_factory_create_voyage_ai(self):
        """Factory.create(config) must return VoyageAIClient by default."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.services.voyage_ai import VoyageAIClient
        from code_indexer.config import Config

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            config = Config()
            provider = EmbeddingProviderFactory.create(config)
            assert isinstance(provider, VoyageAIClient)

    def test_factory_create_cohere_with_provider_name(self):
        """Factory.create(config, provider_name='cohere') must return CohereEmbeddingProvider."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
        from code_indexer.config import Config

        with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
            config = Config()
            provider = EmbeddingProviderFactory.create(config, provider_name="cohere")
            assert isinstance(provider, CohereEmbeddingProvider)

    def test_factory_get_available_providers(self):
        """get_available_providers() must return both providers."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        providers = EmbeddingProviderFactory.get_available_providers()
        assert providers == ["voyage-ai", "cohere"]

    def test_factory_get_configured_providers_both(self):
        """get_configured_providers() must return both when both API keys set."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory
        from code_indexer.config import Config

        with patch.dict(
            os.environ,
            {"VOYAGE_API_KEY": "test-key", "CO_API_KEY": "test-cohere-key"},
        ):
            config = Config()
            providers = EmbeddingProviderFactory.get_configured_providers(config)
            assert "voyage-ai" in providers
            assert "cohere" in providers


class TestSlugSeparator:
    """generate_model_slug uses double-underscore separator."""

    def test_slug_double_underscore_separator(self):
        """generate_model_slug('voyage-ai', 'voyage-code-3') must use __ separator."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        slug = EmbeddingProviderFactory.generate_model_slug(
            "voyage-ai", "voyage-code-3"
        )
        assert slug == "voyage_ai__voyage_code_3"

    def test_slug_cohere_model(self):
        """generate_model_slug('cohere', 'embed-v4.0') must produce correct slug."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        slug = EmbeddingProviderFactory.generate_model_slug("cohere", "embed-v4.0")
        assert slug == "cohere__embed_v4_0"


class TestConfigBackwardCompat:
    """Config backward compatibility for multi-provider support."""

    def test_config_default_provider_is_voyage_ai(self):
        """Config().embedding_provider must default to 'voyage-ai'."""
        from code_indexer.config import Config

        config = Config()
        assert config.embedding_provider == "voyage-ai"

    def test_config_accepts_cohere_provider(self):
        """Config(embedding_provider='cohere') must not raise."""
        from code_indexer.config import Config

        config = Config(embedding_provider="cohere")
        assert config.embedding_provider == "cohere"

    def test_config_has_cohere_field(self):
        """Config().cohere must be a CohereConfig instance."""
        from code_indexer.config import Config, CohereConfig

        config = Config()
        assert isinstance(config.cohere, CohereConfig)

    def test_cohere_config_defaults(self):
        """CohereConfig defaults: model='embed-v4.0', default_dimension=1536."""
        from code_indexer.config import CohereConfig

        cohere_config = CohereConfig()
        assert cohere_config.model == "embed-v4.0"
        assert cohere_config.default_dimension == 1536


class TestCohereBatchSplitting:
    """Batch splitting respects texts_per_request limit."""

    def test_batch_respects_texts_per_request_limit(self, cohere_provider):
        """Sending 200 texts must produce multiple batches of <=96 each."""
        captured_batches = []

        def capture_request(texts, input_type="search_document"):
            captured_batches.append(len(texts))
            return {"embeddings": {"float": [[0.1, 0.2] for _ in range(len(texts))]}}

        with patch.object(
            cohere_provider, "_make_sync_request", side_effect=capture_request
        ):
            with patch.object(cohere_provider, "_count_tokens", return_value=10):
                texts = [f"text {i}" for i in range(200)]
                result = cohere_provider.get_embeddings_batch(
                    texts, embedding_purpose="document"
                )

        # All 200 texts must produce embeddings
        assert len(result) == 200
        # Must have made multiple batch calls
        assert len(captured_batches) >= 2
        # Each batch must respect Cohere's texts_per_request limit (96 per
        # cohere_models.yaml default for embed-v4.0)
        for batch_size in captured_batches:
            assert batch_size <= 96
