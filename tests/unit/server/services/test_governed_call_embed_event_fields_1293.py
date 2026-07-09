"""Story #1293: EmbeddingCacheMetadata is enriched with the fields the shared
emit helper reads (Algorithm 3): provider, model, config_digest, outcome,
role, live_batch_id, embed_key, long_key, shadow_cosine.

governed_call.py's own Path B (no coalescer) constructions populate
outcome/role/provider/config_digest/embed_key deterministically, since these
branches are NEVER part of a coalesced provider HTTP batch (live_batch_id is
always None here). Path A (coalescer.submit()) constructions are untouched in
S1a -- their role/outcome stay None until Story #1293 S1b wires the coalescer
owner/joiner distinction (embedding_coalescer.py, item A3).
"""

from unittest.mock import MagicMock, patch


def _make_provider(provider_name: str = "voyage-ai") -> MagicMock:
    provider = MagicMock()
    provider.get_provider_name.return_value = provider_name
    provider.get_embedding.return_value = [0.1, 0.2, 0.3]
    return provider


class TestEmbeddingCacheMetadataEnrichedFields:
    def test_new_fields_default_to_none(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        meta = EmbeddingCacheMetadata()
        assert meta.provider is None
        assert meta.model is None
        assert meta.config_digest is None
        assert meta.outcome is None
        assert meta.role is None
        assert meta.live_batch_id is None
        assert meta.embed_key is None
        assert meta.long_key is None
        assert meta.shadow_cosine is None

    def test_new_fields_can_be_set(self):
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        meta = EmbeddingCacheMetadata(
            provider="voyage-ai",
            model="voyage-code-3",
            config_digest="d123",
            outcome="miss",
            role="direct",
            live_batch_id=None,
            embed_key="s:d123:hello",
            long_key=False,
            shadow_cosine=0.9,
        )
        assert meta.provider == "voyage-ai"
        assert meta.outcome == "miss"
        assert meta.role == "direct"
        assert meta.embed_key == "s:d123:hello"
        assert meta.shadow_cosine == 0.9


class TestPathBPopulatesOutcomeAndRole:
    """governed_call.py's own (non-coalescer) constructions must set
    outcome/role deterministically, since these rows can NEVER be a
    coalescer owner/joiner (live_batch_id always None here)."""

    def _call(self, provider=None, text="query", **kwargs):
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        if provider is None:
            provider = _make_provider()
        return coalesced_query_embedding(provider, text, **kwargs)

    def test_no_registry_no_cache_sets_direct_miss(self):
        provider = _make_provider("voyage-ai")
        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=None,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.governed_query_embedding",
                    return_value=[1.0, 2.0],
                ):
                    with patch(
                        "code_indexer.server.services.governed_call._digest_for_provider",
                        return_value="digest-no-cache",
                    ):
                        _vec, meta = self._call(provider)

        assert meta.outcome == "miss"
        assert meta.role == "direct"
        assert meta.live_batch_id is None
        assert meta.provider == "voyage-ai"
        assert meta.config_digest == "digest-no-cache"

    def test_bypass_path_sets_bypass_role_direct(self):
        provider = _make_provider("voyage-ai")
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "key-abc"
        mock_cache.qualifier.return_value = MagicMock()

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call._digest_for_provider",
                    return_value="d" * 10,
                ):
                    with patch(
                        "code_indexer.server.services.governed_call.governed_query_embedding",
                        return_value=[10.0, 11.0],
                    ):
                        _vec, meta = self._call(
                            provider, no_embedding_cache_shortcut=True
                        )

        assert meta.outcome == "bypass"
        assert meta.role == "direct"
        assert meta.live_batch_id is None
        assert meta.provider == "voyage-ai"

    def test_serve_with_cache_on_mode_hit_sets_warm_hit(self):
        import struct

        provider = _make_provider("voyage-ai")
        live_vec = [1.0, 2.0, 3.0]
        blob = struct.pack(f"<{len(live_vec)}f", *live_vec)

        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "s:d:hello"
        mock_qualifier = MagicMock()
        mock_qualifier.dimension = len(live_vec)
        mock_cache.qualifier.return_value = mock_qualifier
        mock_cache.lookup.return_value = blob

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call._digest_for_provider",
                    return_value="digest-hit",
                ):
                    _vec, meta = self._call(provider)

        assert meta.outcome == "hit"
        assert meta.role == "warm_hit"
        assert meta.live_batch_id is None
        assert meta.embed_key == "s:d:hello"

    def test_serve_with_cache_on_mode_miss_sets_direct_miss(self):
        provider = _make_provider("voyage-ai")
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "s:d:miss-key"
        mock_qualifier = MagicMock()
        mock_qualifier.dimension = 3
        mock_cache.qualifier.return_value = mock_qualifier
        mock_cache.lookup.return_value = None  # MISS

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call._digest_for_provider",
                    return_value="digest-miss",
                ):
                    with patch(
                        "code_indexer.server.services.governed_call.governed_query_embedding",
                        return_value=[9.0, 9.0, 9.0],
                    ):
                        _vec, meta = self._call(provider)

        assert meta.outcome == "miss"
        assert meta.role == "direct"
        assert meta.live_batch_id is None
        assert meta.embed_key == "s:d:miss-key"

    def test_path_a_coalescer_meta_untouched_role_stays_none(self):
        """Path A (coalescer engaged) is UNTOUCHED in S1a -- role/outcome stay
        whatever the coalescer returned (None today; wired in S1b)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider("voyage-ai")
        mock_coalescer = MagicMock()
        mock_coalescer.submit.return_value = ([7.0, 8.0], EmbeddingCacheMetadata())

        mock_registry = MagicMock()
        mock_registry.get_or_create.return_value = mock_coalescer

        mock_cfg = MagicMock()
        mock_cfg.get_config.return_value.coalesce_enabled = True

        with patch(
            "code_indexer.server.services.governed_call.get_query_embedding_cache",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_coalescer_registry",
                return_value=mock_registry,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.get_config_service",
                    return_value=mock_cfg,
                ):
                    with patch(
                        "code_indexer.server.services.governed_call._digest_for_provider",
                        return_value="digest123",
                    ):
                        _vec, meta = self._call(provider)

        assert meta.role is None
        assert meta.outcome is None
