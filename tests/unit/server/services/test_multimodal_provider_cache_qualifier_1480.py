"""Bug #1480 remediation — real multimodal providers through a REAL, WIRED,
ENABLED (shadow-mode) query-embedding cache.

Context: Bug #1480 wired server-side multimodal query into
``search_service.py`` via ``MultiIndexQueryService.query_with_separate_kwargs()``.
In the default production deployment the server-side query-embedding cache
is enabled in shadow mode. ``governed_call.coalesced_query_embedding``'s
Path B bypass branch (``no_embedding_cache_shortcut=True``) unconditionally
calls ``cache.qualifier(provider)`` -- which requires the FULL
``get_provider_name()`` / ``get_current_model()`` / ``get_model_info()``
contract -- BEFORE recording the shadow miss. Neither
``VoyageMultimodalClient`` nor ``CohereMultimodalClient`` implemented that
full contract, so the multimodal query path raised an ``AttributeError``
in any deployment where the cache is enabled -- the exact production
scenario Bug #1480 was supposed to fix.

The pre-existing ``*_1480.py`` tests mask this: they patch
``MultiIndexQueryService._get_multimodal_provider`` to a compliant fake AND
never wire a real, enabled cache (no server lifespan -> ``get_query_embedding_
cache()`` returns None -> the qualifier path is never reached). This test
uses REAL ``VoyageMultimodalClient`` / ``CohereMultimodalClient`` instances
driven through the REAL ``governed_call.coalesced_query_embedding`` ->
REAL ``QueryEmbeddingCache.qualifier()`` call path, with only the outbound
HTTP embedding call stubbed (network I/O, never the provider contract
methods under test).
"""

from typing import Any, Dict, List, Optional

import pytest

from code_indexer.config import VOYAGE_MULTIMODAL_MODEL, CohereConfig, VoyageAIConfig
from code_indexer.server.services import governed_call
from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache
from code_indexer.services.cohere_multimodal import CohereMultimodalClient
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient


class _InMemoryQueryEmbeddingCacheBackend:
    """Real in-memory dict-backed cache backend -- no mocking of cache I/O.

    Mirrors the established ``_FakeBackend3c`` pattern in
    ``test_coalesced_query_embedding.py``.
    """

    def __init__(self) -> None:
        self._store: Dict[Any, bytes] = {}

    def lookup(self, key, provider, model, dimension) -> Optional[bytes]:
        return self._store.get((key, provider, model, dimension))

    def upsert(self, key, provider, model, dimension, blob, last_used, created_at):
        self._store[(key, provider, model, dimension)] = blob

    def touch_last_used(self, key, provider, model, dimension, ts):
        pass

    def prune_to_max(self, max_entries):
        pass

    def total_entries(self) -> int:
        return len(self._store)


def _make_real_enabled_shadow_cache() -> QueryEmbeddingCache:
    """Real QueryEmbeddingCache wired ON with mode=shadow for both providers
    -- the confirmed default production posture per the Bug #1480 remediation
    brief. ``mode_for``/``enabled_for`` are overridden to bypass the live
    config-service read (this test does not wire a server ConfigService) --
    the SAME technique ``test_coalesced_query_embedding.py`` already uses."""
    backend = _InMemoryQueryEmbeddingCacheBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode="shadow", cohere_mode="shadow"
    )
    cache.mode_for = lambda pname: "shadow"  # type: ignore[method-assign]
    cache.enabled_for = lambda pname: True  # type: ignore[method-assign]
    return cache


@pytest.fixture(autouse=True)
def _wire_real_enabled_cache_no_coalescer():
    """Wire a REAL, ENABLED, shadow-mode cache with NO coalescer registered,
    so ``coalesced_query_embedding`` takes Path B (direct governed call) and
    reaches the ``cache.qualifier(provider)`` call inside the bypass branch
    -- the exact line the Bug #1480 remediation targets."""
    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()
    cache = _make_real_enabled_shadow_cache()
    governed_call.set_query_embedding_cache(cache)
    yield cache
    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()


def _stub_outbound_http_embedding_call(
    monkeypatch, client_cls, vector: List[float]
) -> None:
    """Stub ONLY the outbound HTTP embedding call (network I/O). Every other
    method -- ``get_provider_name``/``get_current_model``/``get_model_info``/
    ``get_embedding`` -- stays the REAL implementation under test."""

    def _fake_get_multimodal_embedding(
        self, text: str, image_paths, input_type: Optional[str] = None
    ) -> List[float]:
        return vector

    monkeypatch.setattr(
        client_cls, "get_multimodal_embedding", _fake_get_multimodal_embedding
    )


class TestMultimodalProviderCacheQualifierContract:
    """Real multimodal clients driven through a real, wired, enabled cache
    must not crash ``cache.qualifier()`` -- Bug #1480 remediation."""

    def test_voyage_multimodal_client_through_enabled_cache(self, monkeypatch):
        monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
        client = VoyageMultimodalClient(VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL))
        _stub_outbound_http_embedding_call(
            monkeypatch, VoyageMultimodalClient, [0.1, 0.2, 0.3, 0.4]
        )

        vec, meta = governed_call.coalesced_query_embedding(
            client,
            "architecture diagram",
            no_embedding_cache_shortcut=True,
        )

        assert vec == [0.1, 0.2, 0.3, 0.4]
        assert meta.key_found is False
        assert meta.embed_key is not None

    def test_cohere_multimodal_client_through_enabled_cache(self, monkeypatch):
        client = CohereMultimodalClient(CohereConfig(model="embed-v4.0"))
        _stub_outbound_http_embedding_call(
            monkeypatch, CohereMultimodalClient, [0.5, 0.6, 0.7, 0.8, 0.9]
        )

        vec, meta = governed_call.coalesced_query_embedding(
            client,
            "architecture diagram",
            no_embedding_cache_shortcut=True,
        )

        assert vec == [0.5, 0.6, 0.7, 0.8, 0.9]
        assert meta.key_found is False
        assert meta.embed_key is not None

    def test_multimodal_and_code_provider_qualifiers_are_distinct(self, monkeypatch):
        """Cache qualifier (and therefore cache key namespace) must be
        distinct between the multimodal provider and the text/code provider
        that shares the same underlying vendor -- multimodal and code
        embeddings must never collide in the cache."""
        from code_indexer.services.voyage_ai import VoyageAIClient

        monkeypatch.setenv("VOYAGE_API_KEY", "test-voyage-key")
        cache = _make_real_enabled_shadow_cache()

        multimodal_client = VoyageMultimodalClient(
            VoyageAIConfig(model=VOYAGE_MULTIMODAL_MODEL)
        )
        code_client = VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))

        multimodal_qualifier = cache.qualifier(multimodal_client)
        code_qualifier = cache.qualifier(code_client)

        assert multimodal_qualifier != code_qualifier
        assert multimodal_qualifier.model != code_qualifier.model

        from code_indexer.server.services.coalescer_registry import (
            _digest_for_provider,
        )

        assert _digest_for_provider(multimodal_client) != _digest_for_provider(
            code_client
        )
