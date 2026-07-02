"""Tests for multi-provider loop in cidx index (Story #620).

Tests verify that:
- get_embedding_providers() returns single-item list for backward compat repos
- resolve_api_key() correctly gates which providers get indexed
- providers missing API keys are skipped
"""

import os
from unittest.mock import patch


class TestMultiProviderIndexGating:
    """Test that provider loop only indexes providers with valid API keys."""

    def test_single_provider_config_gives_one_provider(self):
        """Repos with only embedding_provider (singular) give single-item provider list."""
        from code_indexer.config import Config

        config = Config(embedding_provider="voyage-ai", embedding_providers=None)
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai"]
        assert len(providers) == 1

    def test_multi_provider_config_gives_full_list(self):
        """Repos with embedding_providers list give the full list."""
        from code_indexer.config import Config

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai", "cohere"]
        assert len(providers) == 2

    def test_resolve_api_key_gates_voyage_ai_indexing(self):
        """Only voyage-ai with VOYAGE_API_KEY set proceeds to indexing."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "v-key"}, clear=False):
            key = EmbeddingProviderFactory.resolve_api_key("voyage-ai")
        assert key == "v-key"

    def test_resolve_api_key_gates_cohere_indexing(self):
        """Only cohere with CO_API_KEY set proceeds to indexing."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {"CO_API_KEY": "c-key"}, clear=False):
            key = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert key == "c-key"

    def test_missing_api_key_blocks_provider(self):
        """Provider with missing API key is blocked from indexing (returns None)."""
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        with patch.dict(os.environ, {}, clear=True):
            key = EmbeddingProviderFactory.resolve_api_key("cohere")
        assert key is None

    def test_provider_loop_skips_providers_without_api_key(self):
        """Provider loop skips providers where resolve_api_key() returns None."""
        from code_indexer.config import Config
        from code_indexer.services.embedding_factory import EmbeddingProviderFactory

        config = Config(embedding_providers=["voyage-ai", "cohere"])
        providers = config.get_embedding_providers()

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "v-key"}, clear=True):
            indexable = [
                p
                for p in providers
                if EmbeddingProviderFactory.resolve_api_key(p) is not None
            ]

        assert "cohere" not in indexable
        assert "voyage-ai" in indexable

    def test_backward_compat_single_provider_repo_no_embedding_providers_key(self):
        """Repos without embedding_providers key fall back gracefully to single provider."""
        from code_indexer.config import Config

        config = Config(embedding_provider="voyage-ai")
        assert config.embedding_providers is None
        providers = config.get_embedding_providers()
        assert providers == ["voyage-ai"]

    def test_skip_warning_logged_for_missing_api_key(self, caplog):
        """Warning is logged when a provider is skipped due to missing API key."""
        import logging
        from code_indexer.cli import _log_skipped_provider_warning

        with caplog.at_level(logging.WARNING, logger="code_indexer.cli"):
            with patch.dict(os.environ, {}, clear=True):
                _log_skipped_provider_warning("cohere")

        assert any("cohere" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)
