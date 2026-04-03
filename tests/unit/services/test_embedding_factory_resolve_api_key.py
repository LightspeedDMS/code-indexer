"""Tests for EmbeddingProviderFactory.resolve_api_key() (Story #620)."""

import os
from unittest.mock import patch


class TestResolveApiKey:
    """Test EmbeddingProviderFactory.resolve_api_key() maps providers to env vars."""

    def test_resolve_api_key_voyage_ai(self):
        """resolve_api_key('voyage-ai') returns VOYAGE_API_KEY env var value."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-voyage-key"}):
            result = EmbeddingProviderFactory.resolve_api_key("voyage-ai")
        assert result == "test-voyage-key"

    def test_resolve_api_key_cohere(self):
        """resolve_api_key('cohere') returns CO_API_KEY env var value."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"CO_API_KEY": "test-cohere-key"}):
            result = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert result == "test-cohere-key"

    def test_resolve_api_key_unknown_provider(self):
        """resolve_api_key() returns None for unknown provider names."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        result = EmbeddingProviderFactory.resolve_api_key("unknown-provider")
        assert result is None

    def test_resolve_api_key_missing_env_var(self):
        """resolve_api_key() returns None when the env var is not set."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        env_without_key = {k: v for k, v in os.environ.items() if k != "VOYAGE_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            result = EmbeddingProviderFactory.resolve_api_key("voyage-ai")
        assert result is None

    def test_resolve_api_key_missing_cohere_env_var(self):
        """resolve_api_key('cohere') returns None when CO_API_KEY is not set."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        env_without_key = {k: v for k, v in os.environ.items() if k != "CO_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            result = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert result is None
