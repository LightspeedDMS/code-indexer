"""Tests for EmbeddingProviderFactory with ServerConfig objects.

Bug #608: create() and get_provider_model_info() crash with AttributeError when
called with a ServerConfig (which has flat fields voyageai_api_key/cohere_api_key
instead of nested .voyage_ai/.cohere sub-objects).
"""

from unittest.mock import MagicMock, patch

from code_indexer.services.embedding_factory import EmbeddingProviderFactory


def _make_server_config(provider: str) -> MagicMock:
    """Build a ServerConfig-like mock: has embedding_provider but NO nested config."""
    cfg = MagicMock(spec=[])  # spec=[] means no attributes exist by default
    cfg.embedding_provider = provider
    return cfg


def _make_cli_config_voyage() -> MagicMock:
    """Build a CLI-Config-like mock that has .voyage_ai sub-object."""
    from code_indexer.config import VoyageAIConfig

    cfg = MagicMock(spec=[])
    cfg.embedding_provider = "voyage-ai"
    cfg.voyage_ai = VoyageAIConfig()
    return cfg


def _make_cli_config_cohere() -> MagicMock:
    """Build a CLI-Config-like mock that has .cohere sub-object."""
    from code_indexer.config import CohereConfig

    cfg = MagicMock(spec=[])
    cfg.embedding_provider = "cohere"
    cfg.cohere = CohereConfig()
    return cfg


class TestCreateWithServerConfig:
    """create() must not raise AttributeError when config lacks nested sub-objects."""

    def test_create_voyage_ai_with_server_config_returns_voyage_client(self) -> None:
        """create('voyage-ai') with ServerConfig (no .voyage_ai) returns VoyageAIClient."""
        cfg = _make_server_config("voyage-ai")

        with patch(
            "code_indexer.services.embedding_factory.VoyageAIClient.__init__",
            return_value=None,
        ) as mock_init:
            result = EmbeddingProviderFactory.create(cfg, provider_name="voyage-ai")

        from code_indexer.services.voyage_ai import VoyageAIClient

        assert isinstance(result, VoyageAIClient)
        mock_init.assert_called_once()
        # First arg to __init__ must be a VoyageAIConfig default instance (not crashing)
        from code_indexer.config import VoyageAIConfig

        call_args = mock_init.call_args[0]
        assert isinstance(call_args[0], VoyageAIConfig)

    def test_create_cohere_with_server_config_returns_cohere_provider(self) -> None:
        """create('cohere') with ServerConfig (no .cohere) returns CohereEmbeddingProvider."""
        cfg = _make_server_config("cohere")

        with patch(
            "code_indexer.services.cohere_embedding.CohereEmbeddingProvider.__init__",
            return_value=None,
        ) as mock_init:
            result = EmbeddingProviderFactory.create(cfg, provider_name="cohere")

        from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        assert isinstance(result, CohereEmbeddingProvider)
        mock_init.assert_called_once()
        from code_indexer.config import CohereConfig

        call_args = mock_init.call_args[0]
        assert isinstance(call_args[0], CohereConfig)


class TestGetProviderModelInfoWithServerConfig:
    """get_provider_model_info() must not raise AttributeError with ServerConfig."""

    def test_get_provider_model_info_voyage_ai_server_config_returns_dict(
        self,
    ) -> None:
        """get_provider_model_info for voyage-ai with ServerConfig returns dict."""
        cfg = _make_server_config("voyage-ai")

        mock_provider = MagicMock()
        mock_provider.get_current_model.return_value = "voyage-code-3"
        mock_provider.get_model_info.return_value = {
            "dimensions": 1024,
            "model": "voyage-code-3",
        }

        with patch(
            "code_indexer.services.embedding_factory.VoyageAIClient.__init__",
            return_value=None,
        ):
            with patch(
                "code_indexer.services.embedding_factory.VoyageAIClient.get_current_model",
                return_value="voyage-code-3",
            ):
                with patch(
                    "code_indexer.services.embedding_factory.VoyageAIClient.get_model_info",
                    return_value={"dimensions": 1024, "model": "voyage-code-3"},
                ):
                    result = EmbeddingProviderFactory.get_provider_model_info(
                        cfg, provider_name="voyage-ai"
                    )

        assert isinstance(result, dict)
        assert result["provider_name"] == "voyage-ai"
        assert result["model_name"] == "voyage-code-3"
        assert result["dimensions"] == 1024

    def test_get_provider_model_info_cohere_server_config_returns_dict(self) -> None:
        """get_provider_model_info for cohere with ServerConfig returns dict."""
        cfg = _make_server_config("cohere")

        with patch(
            "code_indexer.services.cohere_embedding.CohereEmbeddingProvider.__init__",
            return_value=None,
        ):
            with patch(
                "code_indexer.services.cohere_embedding.CohereEmbeddingProvider.get_current_model",
                return_value="embed-v4.0",
            ):
                with patch(
                    "code_indexer.services.cohere_embedding.CohereEmbeddingProvider.get_model_info",
                    return_value={"dimensions": 1536, "model": "embed-v4.0"},
                ):
                    result = EmbeddingProviderFactory.get_provider_model_info(
                        cfg, provider_name="cohere"
                    )

        assert isinstance(result, dict)
        assert result["provider_name"] == "cohere"
        assert result["model_name"] == "embed-v4.0"
        assert result["dimensions"] == 1536


class TestCreateWithCliConfig:
    """Regression guard: create() still works when config has nested sub-objects."""

    def test_create_voyage_ai_with_cli_config_uses_voyage_ai_subconfig(self) -> None:
        """create('voyage-ai') with CLI config (has .voyage_ai) passes it through."""
        cfg = _make_cli_config_voyage()
        voyage_sub = cfg.voyage_ai

        with patch(
            "code_indexer.services.embedding_factory.VoyageAIClient.__init__",
            return_value=None,
        ) as mock_init:
            EmbeddingProviderFactory.create(cfg, provider_name="voyage-ai")

        call_args = mock_init.call_args[0]
        assert call_args[0] is voyage_sub

    def test_create_cohere_with_cli_config_uses_cohere_subconfig(self) -> None:
        """create('cohere') with CLI config (has .cohere) passes it through."""
        cfg = _make_cli_config_cohere()
        cohere_sub = cfg.cohere

        with patch(
            "code_indexer.services.cohere_embedding.CohereEmbeddingProvider.__init__",
            return_value=None,
        ) as mock_init:
            EmbeddingProviderFactory.create(cfg, provider_name="cohere")

        call_args = mock_init.call_args[0]
        assert call_args[0] is cohere_sub
