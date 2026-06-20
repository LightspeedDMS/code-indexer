"""Tests for EmbeddingCacheMetadata and the tuple return from coalesced_query_embedding.

Issue #1159: coalesced_query_embedding must return Tuple[List[float], EmbeddingCacheMetadata]
at EVERY exit branch so callers can extract cache telemetry without extra API calls.

audit_ctx must remain a valid parameter (not removed).
"""

from typing import Any, Dict
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: minimal provider stub
# ---------------------------------------------------------------------------


def _make_provider(provider_name: str = "voyage-ai") -> MagicMock:
    """Build a minimal EmbeddingProvider mock."""
    provider = MagicMock()
    provider.get_provider_name.return_value = provider_name
    provider.get_embedding.return_value = [0.1, 0.2, 0.3]
    return provider


# ---------------------------------------------------------------------------
# Tests: EmbeddingCacheMetadata dataclass
# ---------------------------------------------------------------------------


class TestEmbeddingCacheMetadata:
    def test_defaults_are_all_none(self):
        """EmbeddingCacheMetadata has all-None defaults."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        meta = EmbeddingCacheMetadata()
        assert meta.key_found is None
        assert meta.cache_mode is None
        assert meta.provider_latency_ms is None

    def test_fields_can_be_set(self):
        """EmbeddingCacheMetadata fields can be set."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        meta = EmbeddingCacheMetadata(
            key_found=True,
            cache_mode="on",
            provider_latency_ms=42,
        )
        assert meta.key_found is True
        assert meta.cache_mode == "on"
        assert meta.provider_latency_ms == 42

    def test_key_found_false(self):
        """EmbeddingCacheMetadata key_found=False (cache miss) works."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        meta = EmbeddingCacheMetadata(key_found=False, cache_mode="shadow")
        assert meta.key_found is False
        assert meta.cache_mode == "shadow"
        assert meta.provider_latency_ms is None


# ---------------------------------------------------------------------------
# Tests: coalesced_query_embedding return type
# ---------------------------------------------------------------------------


class TestCoalescedQueryEmbeddingReturnType:
    """coalesced_query_embedding must return Tuple[List[float], EmbeddingCacheMetadata]
    on ALL exit paths (no coalescer + no cache, no coalescer + cache off,
    coalescer path, bypass path, etc.)."""

    def _call(self, provider=None, text="query", **kwargs):
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        if provider is None:
            provider = _make_provider()
        return coalesced_query_embedding(provider, text, **kwargs)

    def test_returns_tuple_no_registry_no_cache(self):
        """Path: no coalescer registry, no cache → returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
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
                    result = self._call(provider)

        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2
        vec, meta = result
        assert isinstance(vec, list)
        assert isinstance(meta, EmbeddingCacheMetadata)

    def test_returns_tuple_cache_disabled(self):
        """Path: cache present but disabled → returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = False

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.governed_query_embedding",
                    return_value=[3.0, 4.0],
                ):
                    result = self._call(provider)

        assert isinstance(result, tuple)
        vec, meta = result
        assert vec == [3.0, 4.0]
        assert isinstance(meta, EmbeddingCacheMetadata)

    def test_returns_tuple_cache_mode_off(self):
        """Path: cache mode=off → returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "off"

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.governed_query_embedding",
                    return_value=[5.0],
                ):
                    result = self._call(provider)

        assert isinstance(result, tuple)
        _, meta = result
        assert isinstance(meta, EmbeddingCacheMetadata)

    def test_returns_tuple_with_coalescer(self):
        """Path A: coalescer present → submit() used, returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
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
                        result = self._call(provider)

        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        vec, meta = result
        assert vec == [7.0, 8.0]
        assert isinstance(meta, EmbeddingCacheMetadata)

    def test_returns_tuple_long_key(self):
        """Path: key exceeds 256-char cap → returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = None  # long key → None

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.get_query_embedding_cache_metrics",
                    return_value=None,
                ):
                    with patch(
                        "code_indexer.server.services.governed_call._digest_for_provider",
                        return_value="d" * 10,
                    ):
                        with patch(
                            "code_indexer.server.services.governed_call.governed_query_embedding",
                            return_value=[9.0],
                        ):
                            result = self._call(provider)

        assert isinstance(result, tuple)
        _, meta = result
        assert isinstance(meta, EmbeddingCacheMetadata)

    def test_returns_tuple_bypass_path(self):
        """Path B: no_embedding_cache_shortcut=True → bypass path returns (vector, metadata)."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata

        provider = _make_provider()
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
                    "code_indexer.server.services.governed_call.get_query_embedding_cache_metrics",
                    return_value=None,
                ):
                    with patch(
                        "code_indexer.server.services.governed_call._digest_for_provider",
                        return_value="d" * 10,
                    ):
                        with patch(
                            "code_indexer.server.services.governed_call.governed_query_embedding",
                            return_value=[10.0, 11.0],
                        ):
                            result = self._call(
                                provider, no_embedding_cache_shortcut=True
                            )

        assert isinstance(result, tuple)
        vec, meta = result
        assert isinstance(meta, EmbeddingCacheMetadata)
        # bypass path: cache read skipped but write fired; key_found=False
        assert meta.key_found is False


# ---------------------------------------------------------------------------
# Tests: audit_ctx is still accepted (not removed)
# ---------------------------------------------------------------------------


class TestAuditCtxNotRemoved:
    def test_audit_ctx_parameter_accepted(self):
        """coalesced_query_embedding still accepts audit_ctx parameter."""
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        provider = _make_provider()
        audit_ctx: Dict[str, Any] = {}

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
                    return_value=[1.0],
                ):
                    # Should not raise TypeError for unexpected keyword argument
                    result = coalesced_query_embedding(
                        provider, "query", audit_ctx=audit_ctx
                    )

        assert isinstance(result, tuple)

    def test_audit_ctx_none_is_valid(self):
        """audit_ctx=None is the default and must not cause errors."""
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        provider = _make_provider()

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
                    return_value=[2.0],
                ):
                    result = coalesced_query_embedding(
                        provider, "query", audit_ctx=None
                    )

        assert isinstance(result, tuple)

    def test_audit_ctx_is_populated_on_sampled_hit(self):
        """audit_ctx dict is populated when _serve_with_cache hits and sampling fires."""
        import struct
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
        )

        provider = _make_provider()
        live_vec = [1.0, 2.0, 3.0]
        blob = struct.pack(f"<{len(live_vec)}f", *live_vec)

        mock_cache = MagicMock()
        mock_cache.enabled_for.return_value = True
        mock_cache.mode_for.return_value = "on"
        mock_cache.build_key_for_provider.return_value = "test-key"
        mock_qualifier = MagicMock()
        mock_qualifier.dimension = len(live_vec)
        mock_cache.qualifier.return_value = mock_qualifier
        mock_cache.lookup.return_value = blob  # cache HIT

        audit_ctx: Dict[str, Any] = {}

        with patch(
            "code_indexer.server.services.governed_call.get_coalescer_registry",
            return_value=None,
        ):
            with patch(
                "code_indexer.server.services.governed_call.get_query_embedding_cache",
                return_value=mock_cache,
            ):
                with patch(
                    "code_indexer.server.services.governed_call.get_query_embedding_cache_metrics",
                    return_value=None,
                ):
                    with patch(
                        "code_indexer.server.services.governed_call._digest_for_provider",
                        return_value="digest-x",
                    ):
                        with patch(
                            "code_indexer.server.services.governed_call._audit_sample_rate_for",
                            return_value=1.0,  # 100% sampling so audit always fires
                        ):
                            with patch(
                                "code_indexer.server.services.governed_call.random.random",
                                return_value=0.0,  # always below rate
                            ):
                                result = coalesced_query_embedding(
                                    provider,
                                    "query",
                                    audit_ctx=audit_ctx,
                                )

        assert isinstance(result, tuple)
        # audit_ctx should have been populated by _serve_with_cache
        assert audit_ctx.get("sampled") is True


# ---------------------------------------------------------------------------
# Tests: EmbeddingCoalescer.submit() returns Tuple[List[float], EmbeddingCacheMetadata]
# Root Cause 1 fix (Issue #1159): submit() must return a tuple so that
# coalesced_query_embedding Path A can propagate real cache metadata instead of
# constructing an all-None EmbeddingCacheMetadata().
# ---------------------------------------------------------------------------


class TestCoalescerSubmitReturnsTuple:
    """EmbeddingCoalescer.submit() must return Tuple[List[float], EmbeddingCacheMetadata].

    Before the fix, submit() returned List[float], so coalesced_query_embedding
    Path A always produced EmbeddingCacheMetadata() with all-None fields.
    After the fix, submit() returns (vec, meta) so real metadata flows through.
    """

    def _make_coalescer(self, provider=None):
        """Build a real EmbeddingCoalescer with a fake provider and governor."""
        from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
        from code_indexer.server.services.provider_concurrency_governor import (
            ProviderConcurrencyGovernor,
        )

        if provider is None:
            provider = _make_provider()
            provider._get_model_token_limit = lambda: 120000
            provider._count_tokens_accurately = lambda t: len(t.split())
            provider.get_embeddings_batch = lambda texts, **kw: [
                [0.1, 0.2] for _ in texts
            ]

        gov = ProviderConcurrencyGovernor(max_concurrency=4)
        return EmbeddingCoalescer(
            "voyage:embed",
            provider,
            governor=gov,
            acquire_timeout=5.0,
        )

    def test_submit_returns_tuple(self):
        """submit() must return a 2-tuple (vector, metadata), not a bare list."""
        from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
        from code_indexer.server.services import governed_call

        # Ensure no cache installed (pure live path)
        original = governed_call._query_embedding_cache
        governed_call._query_embedding_cache = None
        try:
            coalescer = self._make_coalescer()
            result = coalescer.submit("test query for tuple return")
        finally:
            governed_call._query_embedding_cache = original

        assert isinstance(result, tuple), (
            f"submit() must return a tuple, got {type(result).__name__}. "
            "Root Cause 1: submit() returns List[float] instead of Tuple[List[float], EmbeddingCacheMetadata]"
        )
        assert len(result) == 2, f"Expected 2-tuple, got {len(result)}-tuple"
        vec, meta = result
        assert isinstance(vec, list), (
            f"First element must be List[float], got {type(vec)}"
        )
        assert isinstance(meta, EmbeddingCacheMetadata), (
            f"Second element must be EmbeddingCacheMetadata, got {type(meta)}"
        )

    def test_coalesced_query_embedding_path_a_propagates_real_metadata(
        self, monkeypatch
    ):
        """Path A (coalescer present): coalesced_query_embedding must propagate
        real EmbeddingCacheMetadata from submit(), not construct a blank all-None one.

        This is the core Root Cause 1 production failure: in server mode (Path A),
        cache telemetry is always all-None because submit() returns List[float]
        and coalesced_query_embedding wraps it with EmbeddingCacheMetadata().
        """
        from code_indexer.server.services.governed_call import (
            EmbeddingCacheMetadata,
            coalesced_query_embedding,
        )
        from code_indexer.server.services.coalescer_registry import (
            CoalescerRegistry,
            set_coalescer_registry,
            clear_coalescer_registry,
        )
        import code_indexer.server.services.governed_call as gc_mod

        # Build a fake coalescer that returns (vec, metadata) — the real tuple
        expected_meta = EmbeddingCacheMetadata(
            key_found=True, cache_mode="on", provider_latency_ms=None
        )
        fake_vec = [1.0, 2.0, 3.0]

        class _FakeCoalescerWithMeta:
            def __init__(self):
                self.submitted = []

            def submit(self, text, embedding_purpose="query", **kw):
                self.submitted.append(text)
                # Returns tuple — the fixed form
                return (fake_vec, expected_meta)

        fake_coalescer = _FakeCoalescerWithMeta()

        reg = CoalescerRegistry.__new__(CoalescerRegistry)
        reg._coalescers = {}
        reg.get_or_create = lambda lane, digest, provider: fake_coalescer

        set_coalescer_registry(reg)
        try:
            monkeypatch.setattr(gc_mod, "get_query_embedding_cache", lambda: None)
            monkeypatch.setattr(
                gc_mod,
                "get_config_service",
                lambda: type(
                    "C",
                    (),
                    {
                        "get_config": lambda self: type(
                            "CC", (), {"coalesce_enabled": True}
                        )()
                    },
                )(),
                raising=False,
            )
            monkeypatch.setattr(gc_mod, "_digest_for_provider", lambda p: "testdigest")

            provider = _make_provider()
            result = coalesced_query_embedding(provider, "test path A metadata")
        finally:
            clear_coalescer_registry()

        assert isinstance(result, tuple)
        vec, meta = result
        assert vec == fake_vec
        # Key assertion: metadata must NOT be all-None — it must be the real one from submit()
        assert meta.key_found is True, (
            "Path A metadata must not be all-None. "
            "Root Cause 1: coalesced_query_embedding constructs EmbeddingCacheMetadata() "
            "instead of unpacking (vec, meta) from submit()."
        )
        assert meta.cache_mode == "on"
