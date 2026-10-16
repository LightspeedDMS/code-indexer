"""EmbeddingProviderFactory.get_configured_providers reads the VoyageAI key
from the environment only.

``VoyageAIConfig`` has no ``api_key`` field (the VoyageAI key lives only in
VOYAGE_API_KEY), so the check must not read one from the config: with
VOYAGE_API_KEY unset that read raised AttributeError for every real
``Config``, breaking each caller (the dual-provider query strategy check, the
``cidx index --index-commits`` provider count, golden-repo registration's
``embedding_providers`` write). The Cohere check reads CO_API_KEY or the CLI
config's ``cohere.api_key``.
"""

from __future__ import annotations

import pytest

from code_indexer.config import Config
from code_indexer.services.embedding_factory import EmbeddingProviderFactory


@pytest.fixture(autouse=True)
def no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("CO_API_KEY", raising=False)


def _config_with_cohere_key() -> Config:
    config = Config()
    config.cohere.api_key = "example-cohere-key"
    return config


class TestVoyageKeyUnset:
    def test_default_config_lists_no_provider(self) -> None:
        assert EmbeddingProviderFactory.get_configured_providers(Config()) == []

    def test_cohere_key_in_config_lists_cohere_only(self) -> None:
        providers = EmbeddingProviderFactory.get_configured_providers(
            _config_with_cohere_key()
        )

        assert providers == ["cohere"]

    def test_cohere_key_in_env_lists_cohere_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CO_API_KEY", "example-cohere-key")

        assert EmbeddingProviderFactory.get_configured_providers(Config()) == ["cohere"]


class TestVoyageKeySet:
    def test_voyage_env_key_lists_voyage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "example-voyage-key")

        assert EmbeddingProviderFactory.get_configured_providers(Config()) == [
            "voyage-ai"
        ]

    def test_both_keys_list_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VOYAGE_API_KEY", "example-voyage-key")

        providers = EmbeddingProviderFactory.get_configured_providers(
            _config_with_cohere_key()
        )

        assert providers == ["voyage-ai", "cohere"]
