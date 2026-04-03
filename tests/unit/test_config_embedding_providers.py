"""Tests for Config.embedding_providers field and get_embedding_providers() method (Story #620)."""


class TestGetEmbeddingProviders:
    """Test Config.get_embedding_providers() returns the correct list."""

    def test_get_embedding_providers_returns_list_when_set(self):
        """get_embedding_providers() returns the embedding_providers list when explicitly set."""
        from code_indexer.config import Config

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        result = config.get_embedding_providers()
        assert result == ["voyage-ai", "cohere"]

    def test_get_embedding_providers_falls_back_to_singular(self):
        """get_embedding_providers() falls back to [embedding_provider] when embedding_providers is None."""
        from code_indexer.config import Config

        config = Config(embedding_provider="voyage-ai", embedding_providers=None)
        result = config.get_embedding_providers()
        assert result == ["voyage-ai"]

    def test_get_embedding_providers_falls_back_to_cohere_singular(self):
        """get_embedding_providers() falls back to [embedding_provider] for cohere as well."""
        from code_indexer.config import Config

        config = Config(embedding_provider="cohere", embedding_providers=None)
        result = config.get_embedding_providers()
        assert result == ["cohere"]

    def test_config_accepts_embedding_providers_field(self):
        """Config model accepts the embedding_providers field without validation errors."""
        from code_indexer.config import Config

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        assert config.embedding_providers == ["voyage-ai", "cohere"]

    def test_get_embedding_providers_single_provider_list(self):
        """get_embedding_providers() works with a single-item list."""
        from code_indexer.config import Config

        config = Config(embedding_providers=["voyage-ai"])
        result = config.get_embedding_providers()
        assert result == ["voyage-ai"]
