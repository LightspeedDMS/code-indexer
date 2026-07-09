"""Bug found via Story #1291 real E2E: _build_query_provider_for_embedder must
resolve a Cohere-family temporal embedder (e.g. "embed-v4.0") to a
CohereEmbeddingProvider, not a VoyageAIClient.

The pre-#1291 implementation gated on `embedder_name.startswith("cohere")`,
written when no Cohere temporal adapter existed. Story #1291 registers the
real adapter as "embed-v4.0" (matching CohereConfig.model / the production
Cohere model name), which does NOT start with "cohere" -- so the stale check
silently misrouted embed-v4.0 queries to a VoyageAIClient, which then failed
(the string "embed-v4.0" is not a valid Voyage model).
"""

import os
from unittest.mock import patch

import pytest

from code_indexer.config import CohereConfig, VoyageAIConfig
from code_indexer.services.cohere_embedding import CohereEmbeddingProvider
from code_indexer.services.voyage_ai import VoyageAIClient
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _build_query_provider_for_embedder,
)


class _FakeConfig:
    def __init__(self) -> None:
        self.voyage_ai = VoyageAIConfig(model="voyage-code-3")
        self.cohere = CohereConfig(model="embed-v4.0")
        self.embedding_provider = "voyage-ai"


@pytest.fixture
def voyage_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield


@pytest.fixture
def cohere_key():
    with patch.dict(os.environ, {"CO_API_KEY": "PLACEHOLDER"}):
        yield


class TestQueryProviderResolvesCorrectFamily:
    def test_cohere_embedder_resolves_to_cohere_provider(self, cohere_key):
        provider = _build_query_provider_for_embedder(_FakeConfig(), "embed-v4.0")
        assert isinstance(provider, CohereEmbeddingProvider), (
            f"expected CohereEmbeddingProvider for 'embed-v4.0', got {type(provider)}"
        )
        assert provider.get_current_model() == "embed-v4.0"

    def test_voyage_embedder_resolves_to_voyage_client(self, voyage_key):
        provider = _build_query_provider_for_embedder(_FakeConfig(), "voyage-context-4")
        assert isinstance(provider, VoyageAIClient)
        assert provider.get_current_model() == "voyage-context-4"


class TestQueryProviderKeylessAdapterFailsLoud:
    """Code review Finding 3 (LOW) on Story #1291: a registered-but-keyless
    adapter (e.g. StandardTemporalEmbedder with no Cohere API key configured)
    must raise a typed, clear error from _build_query_provider_for_embedder --
    NEVER return a bare None. Returning None lets the caller construct a
    TemporalSearchService(embedding_provider=None) that only fails much
    later with an opaque AttributeError deep inside query_temporal(), instead
    of failing loud at the resolution boundary with an actionable message
    (Messi #13 anti-silent-failure)."""

    def test_keyless_cohere_adapter_raises_typed_error_not_none(self, monkeypatch):
        monkeypatch.delenv("CO_API_KEY", raising=False)
        config = _FakeConfig()
        config.cohere = CohereConfig(model="embed-v4.0")  # no api_key configured

        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            TemporalEmbedderUnavailableError,
        )

        with pytest.raises(TemporalEmbedderUnavailableError) as exc_info:
            _build_query_provider_for_embedder(config, "embed-v4.0")

        assert "embed-v4.0" in str(exc_info.value), (
            f"error message must name the unavailable embedder: {exc_info.value}"
        )
