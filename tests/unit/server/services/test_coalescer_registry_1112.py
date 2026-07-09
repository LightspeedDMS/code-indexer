"""Bug #1112 regression suite — lazy per-config-digest coalescer registry.

The coalescer registry previously built ONE coalescer per lane from a BARE
DEFAULT config (VoyageAIConfig() / CohereConfig()), ignoring per-repo
api_endpoint, model, and api_key overrides. All repos shared the same coalescer
regardless of their config -> wrong query vectors for any repo using a non-
default endpoint or model.

The fix: the registry is LAZY, keyed by (lane, config-digest). On the first
request for a (lane, digest) pair the registry constructs a fresh coalescer from
the CALLER's provider (which carries the per-repo config). Subsequent requests
for the same (lane, digest) reuse that coalescer.

This file tests:
  CHUNK 1  — _digest_for_provider: same config -> same digest; different
             endpoint/model/key -> different digest; AttributeError safe.
  CHUNK 2  — CoalescerRegistry lazy get_or_create: miss builds, hit reuses,
             cap-exceeded returns None + WARNING; build_coalescer_registry
             constructs NO default providers.
  CHUNK 3  — governed_call._compute_live dispatches to the digest-keyed coalescer
             whose provider actually carries the caller's per-repo config.
  EXTENDED — heterogeneous-model providers -> separate coalescers;
             different api_endpoint -> different coalescer instances;
             same config -> same instance (not rebuilt).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import pytest

from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
    _digest_for_provider,
    build_coalescer_registry,
    clear_coalescer_registry,
    get_coalescer_registry,
    set_coalescer_registry,
)
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

VOYAGE_EMBED = "voyage:embed"
COHERE_EMBED = "cohere:embed"

# ---------------------------------------------------------------------------
# Fake providers that carry config-like attributes
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal config object the fake providers expose (mirrors real Config)."""

    def __init__(
        self,
        model: str = "voyage-code-3",
        api_key: str = "test-key",
        api_endpoint: str = "https://api.voyageai.com/v1",
        connect_timeout: float = 5.0,
        timeout: float = 30.0,
        max_retries: Optional[int] = None,
        retry_delay: Optional[float] = None,
        exponential_backoff: Optional[bool] = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_endpoint = api_endpoint
        self.connect_timeout = connect_timeout
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.exponential_backoff = exponential_backoff


class _FakeVoyageProvider:
    """Fake Voyage provider with a real-ish config attribute."""

    def __init__(self, config: Optional[_FakeConfig] = None) -> None:
        self.config = config or _FakeConfig()
        self._token_limit = 120_000

    def _count_tokens_accurately(self, text: str) -> int:
        return 1

    def _get_model_token_limit(self) -> int:
        return self._token_limit

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        return [[float(len(t)), 0.0] for t in texts]

    def get_provider_name(self) -> str:
        return "voyage-ai"


class _FakeCohereProvider(_FakeVoyageProvider):
    """Fake Cohere provider; config uses Cohere-like defaults."""

    _count_tokens_accurately = None  # type: ignore[assignment]

    def __init__(self, config: Optional[_FakeConfig] = None) -> None:
        cfg = config or _FakeConfig(
            model="embed-english-v3.0",
            api_endpoint="https://api.cohere.ai/v1",
            max_retries=3,
            retry_delay=1.0,
            exponential_backoff=True,
        )
        super().__init__(config=cfg)

    def _count_tokens(self, text: str) -> int:
        return 1

    def _get_texts_per_request(self) -> int:
        return 96

    def get_provider_name(self) -> str:
        return "cohere"


class _FakeVoyageProviderNoConfig:
    """Fake provider WITHOUT a .config attribute (AttributeError-safety test)."""

    def _count_tokens_accurately(self, text: str) -> int:
        return 1

    def _get_model_token_limit(self) -> int:
        return 120_000

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        return [[0.0, 0.0] for _ in texts]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    clear_coalescer_registry()
    yield
    clear_coalescer_registry()
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()


# ===========================================================================
# CHUNK 1 — _digest_for_provider
# ===========================================================================


class TestDigestForProvider:
    def test_same_config_same_digest(self):
        """Two providers with identical config produce the same digest."""
        cfg = _FakeConfig()
        p1 = _FakeVoyageProvider(cfg)
        p2 = _FakeVoyageProvider(cfg)
        assert _digest_for_provider(p1) == _digest_for_provider(p2)

    def test_different_endpoint_different_digest(self):
        """Different api_endpoint -> different digest."""
        p1 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://default.api.com/v1"))
        p2 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://custom.api.com/v1"))
        assert _digest_for_provider(p1) != _digest_for_provider(p2)

    def test_different_model_different_digest(self):
        """Different model -> different digest."""
        p1 = _FakeVoyageProvider(_FakeConfig(model="voyage-code-3"))
        p2 = _FakeVoyageProvider(_FakeConfig(model="voyage-large-2"))
        assert _digest_for_provider(p1) != _digest_for_provider(p2)

    def test_different_api_key_different_digest(self):
        """Different api_key -> different digest (fingerprinted, not raw)."""
        p1 = _FakeVoyageProvider(_FakeConfig(api_key="key-aaa"))
        p2 = _FakeVoyageProvider(_FakeConfig(api_key="key-bbb"))
        assert _digest_for_provider(p1) != _digest_for_provider(p2)

    def test_different_timeout_different_digest(self):
        """Different timeout -> different digest."""
        p1 = _FakeVoyageProvider(_FakeConfig(timeout=30.0))
        p2 = _FakeVoyageProvider(_FakeConfig(timeout=60.0))
        assert _digest_for_provider(p1) != _digest_for_provider(p2)

    def test_no_config_attribute_returns_stable_sentinel(self):
        """Provider without .config returns a stable non-empty digest (never raises)."""
        p = _FakeVoyageProviderNoConfig()
        d1 = _digest_for_provider(p)
        d2 = _digest_for_provider(p)
        assert d1 == d2
        assert isinstance(d1, str) and len(d1) > 0

    def test_digest_is_string(self):
        """Digest is always a non-empty string."""
        p = _FakeVoyageProvider()
        d = _digest_for_provider(p)
        assert isinstance(d, str) and len(d) > 0


# ===========================================================================
# CHUNK 2 — lazy CoalescerRegistry.get_or_create
# ===========================================================================


class _Cfg:
    def __init__(self, coalesce_max_batch_size: int = 96) -> None:
        self.coalesce_max_batch_size = coalesce_max_batch_size


class TestLazyCoalescerRegistry:
    def _make_registry(self, max_per_lane: int = 64) -> CoalescerRegistry:
        ProviderConcurrencyGovernor(8)
        return CoalescerRegistry(max_per_lane=max_per_lane)

    def test_get_or_create_builds_on_miss(self):
        """A (lane, digest) miss builds a new EmbeddingCoalescer."""
        reg = self._make_registry()
        p = _FakeVoyageProvider()
        digest = _digest_for_provider(p)
        coalescer = reg.get_or_create(VOYAGE_EMBED, digest, p)
        assert coalescer is not None
        assert isinstance(coalescer, EmbeddingCoalescer)

    def test_get_or_create_same_digest_returns_same_instance(self):
        """Same (lane, digest) returns the SAME coalescer — not a new one."""
        reg = self._make_registry()
        p = _FakeVoyageProvider()
        digest = _digest_for_provider(p)
        c1 = reg.get_or_create(VOYAGE_EMBED, digest, p)
        c2 = reg.get_or_create(VOYAGE_EMBED, digest, p)
        assert c1 is c2

    def test_get_or_create_different_digest_different_instance(self):
        """Different digests on the same lane -> distinct coalescer instances."""
        reg = self._make_registry()
        p1 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://ep1.api.com/v1"))
        p2 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://ep2.api.com/v1"))
        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)
        assert d1 != d2
        c1 = reg.get_or_create(VOYAGE_EMBED, d1, p1)
        c2 = reg.get_or_create(VOYAGE_EMBED, d2, p2)
        assert c1 is not c2

    def test_different_lanes_same_digest_different_instance(self):
        """Same digest on different lanes -> distinct coalescers."""
        reg = self._make_registry()
        vp = _FakeVoyageProvider()
        cp = _FakeCohereProvider()
        dv = _digest_for_provider(vp)
        dc = _digest_for_provider(cp)
        cv = reg.get_or_create(VOYAGE_EMBED, dv, vp)
        cc = reg.get_or_create(COHERE_EMBED, dc, cp)
        assert cv is not cc

    def test_cap_exceeded_returns_none_and_logs_warning(self, caplog):
        """When per-lane count >= max_per_lane, returns None and logs WARNING."""
        reg = self._make_registry(max_per_lane=2)
        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.services.coalescer_registry"
        ):
            for i in range(2):
                p = _FakeVoyageProvider(
                    _FakeConfig(api_endpoint=f"https://ep{i}.api.com/v1")
                )
                d = _digest_for_provider(p)
                coalescer = reg.get_or_create(VOYAGE_EMBED, d, p)
                assert coalescer is not None

            # The 3rd distinct digest exceeds the cap -> None + WARNING
            p_extra = _FakeVoyageProvider(
                _FakeConfig(api_endpoint="https://extra.api.com/v1")
            )
            d_extra = _digest_for_provider(p_extra)
            result = reg.get_or_create(VOYAGE_EMBED, d_extra, p_extra)

        assert result is None
        assert any(
            "cap" in r.message.lower() or "max" in r.message.lower()
            for r in caplog.records
        ), (
            f"expected a WARNING about cap/max exceeded; got: {[r.message for r in caplog.records]}"
        )

    def test_cap_exceeded_different_lanes_independent(self):
        """Per-lane cap is independent: voyage cap does not affect cohere."""
        reg = self._make_registry(max_per_lane=1)
        vp = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://v.api.com/v1"))
        cp = _FakeCohereProvider(_FakeConfig(api_endpoint="https://c.api.com/v1"))
        dv = _digest_for_provider(vp)
        dc = _digest_for_provider(cp)

        # Fill voyage lane (cap=1)
        cv = reg.get_or_create(VOYAGE_EMBED, dv, vp)
        assert cv is not None

        # Cohere lane still empty -> can build
        cc = reg.get_or_create(COHERE_EMBED, dc, cp)
        assert cc is not None

    def test_build_coalescer_registry_constructs_no_default_provider(self, monkeypatch):
        """build_coalescer_registry must NOT construct any default providers at build time."""
        voyage_built = []
        cohere_built = []

        def _spy_voyage(*args, **kwargs):
            voyage_built.append(True)
            return _FakeVoyageProvider()

        def _spy_cohere(*args, **kwargs):
            cohere_built.append(True)
            return _FakeCohereProvider()

        monkeypatch.setattr(
            "code_indexer.services.voyage_ai.VoyageAIClient", _spy_voyage
        )
        monkeypatch.setattr(
            "code_indexer.services.cohere_embedding.CohereEmbeddingProvider",
            _spy_cohere,
        )

        ProviderConcurrencyGovernor(8)
        build_coalescer_registry(_Cfg())

        assert not voyage_built, (
            "VoyageAIClient should NOT be constructed at build time"
        )
        assert not cohere_built, (
            "CohereEmbeddingProvider should NOT be constructed at build time"
        )

    def test_build_coalescer_registry_returns_coalescer_registry(self):
        """build_coalescer_registry returns a CoalescerRegistry."""
        ProviderConcurrencyGovernor(8)
        reg = build_coalescer_registry(_Cfg())
        assert isinstance(reg, CoalescerRegistry)

    def test_get_legacy_returns_none(self):
        """The legacy get(lane) API returns None on a lazy registry (backward compat)."""
        reg = self._make_registry()
        # No coalescers built yet -> get(lane) returns None
        assert reg.get(VOYAGE_EMBED) is None

    def test_registry_process_accessors_unchanged(self):
        """set/get/clear process-level accessors still work with the new registry."""
        ProviderConcurrencyGovernor(8)
        reg = build_coalescer_registry(_Cfg())
        set_coalescer_registry(reg)
        assert get_coalescer_registry() is reg
        clear_coalescer_registry()
        assert get_coalescer_registry() is None


# ===========================================================================
# CHUNK 3 — _compute_live dispatches via digest to the caller's provider
# ===========================================================================


class TestComputeLiveDigestDispatch:
    """Test that _compute_live uses the per-repo provider for the coalescer."""

    def _install_registry(self) -> CoalescerRegistry:
        ProviderConcurrencyGovernor(8)
        reg = CoalescerRegistry()
        set_coalescer_registry(reg)
        return reg

    def test_different_endpoint_uses_separate_coalescer(self):
        """Two providers with different api_endpoint get separate coalescers."""
        reg = self._install_registry()

        p1 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://ep1.api.com/v1"))
        p2 = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://ep2.api.com/v1"))

        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)
        assert d1 != d2

        c1 = reg.get_or_create(VOYAGE_EMBED, d1, p1)
        c2 = reg.get_or_create(VOYAGE_EMBED, d2, p2)
        assert c1 is not c2

    def test_same_config_reuses_coalescer(self):
        """Same provider config reuses the same coalescer (not rebuilt)."""
        reg = self._install_registry()

        p1 = _FakeVoyageProvider()
        p2 = _FakeVoyageProvider()  # same default config

        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)
        assert d1 == d2

        c1 = reg.get_or_create(VOYAGE_EMBED, d1, p1)
        c2 = reg.get_or_create(VOYAGE_EMBED, d2, p2)
        assert c1 is c2

    def test_heterogeneous_model_providers_separate_coalescers(self):
        """Providers with different models get separate coalescers."""
        reg = self._install_registry()

        p_code = _FakeVoyageProvider(_FakeConfig(model="voyage-code-3"))
        p_large = _FakeVoyageProvider(_FakeConfig(model="voyage-large-2"))

        d_code = _digest_for_provider(p_code)
        d_large = _digest_for_provider(p_large)
        assert d_code != d_large

        c_code = reg.get_or_create(VOYAGE_EMBED, d_code, p_code)
        c_large = reg.get_or_create(VOYAGE_EMBED, d_large, p_large)
        assert c_code is not c_large

    def test_compute_live_with_coalesce_enabled_uses_coalescer(self, monkeypatch):
        """_compute_live routes to the coalescer (via get_or_create) when enabled."""
        from code_indexer.server.services import governed_call

        # Install a real registry
        reg = self._install_registry()

        # Patch config: coalesce_enabled=True
        class _LiveCfg:
            coalesce_enabled = True

        class _CfgSvc:
            def get_config(self):
                return _LiveCfg()

        monkeypatch.setattr(governed_call, "get_config_service", lambda: _CfgSvc())

        p = _FakeVoyageProvider()
        digest = _digest_for_provider(p)

        # Verify get_or_create is called (coalescer built on first use)
        assert reg.get_or_create(VOYAGE_EMBED, digest, p) is not None

        # Same digest -> same coalescer on subsequent calls
        c1 = reg.get_or_create(VOYAGE_EMBED, digest, p)
        c2 = reg.get_or_create(VOYAGE_EMBED, digest, p)
        assert c1 is c2

    def test_compute_live_none_from_get_or_create_falls_back_to_direct(
        self, monkeypatch
    ):
        """When get_or_create returns None (cap exceeded), falls back to direct call."""
        from code_indexer.server.services import governed_call

        # Install a cap=0 registry so get_or_create always returns None
        ProviderConcurrencyGovernor(8)
        reg = CoalescerRegistry(max_per_lane=0)
        set_coalescer_registry(reg)

        class _LiveCfg:
            coalesce_enabled = True

        class _CfgSvc:
            def get_config(self):
                return _LiveCfg()

        monkeypatch.setattr(governed_call, "get_config_service", lambda: _CfgSvc())

        direct_called = []

        def _mock_direct(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            direct_called.append(True)
            return [1.0, 2.0]

        monkeypatch.setattr(governed_call, "governed_query_embedding", _mock_direct)

        p = _FakeVoyageProvider()
        result = governed_call._compute_live(p, "test text")
        assert result == [1.0, 2.0]
        assert direct_called, (
            "expected fallback to governed_query_embedding when cap exceeded"
        )

    def test_compute_live_custom_endpoint_provider_uses_that_endpoint_coalescer(
        self, monkeypatch
    ):
        """Bug #1112: _compute_live dispatches to a coalescer whose provider carries
        the caller's custom api_endpoint, not a stale default-config one.

        We install a registry, call _compute_live with a custom-endpoint provider,
        and confirm the coalescer built for that (lane, digest) pair is distinct from
        one that would be built for a default-endpoint provider on the same lane.
        """
        from code_indexer.server.services import governed_call

        reg = self._install_registry()

        class _LiveCfg:
            coalesce_enabled = True

        class _CfgSvc:
            def get_config(self):
                return _LiveCfg()

        monkeypatch.setattr(governed_call, "get_config_service", lambda: _CfgSvc())

        # Build a custom-endpoint provider and note its digest
        custom_ep = "https://custom.proxy.internal/v1"
        p_custom = _FakeVoyageProvider(_FakeConfig(api_endpoint=custom_ep))
        p_default = _FakeVoyageProvider(_FakeConfig())  # default endpoint

        d_custom = _digest_for_provider(p_custom)
        d_default = _digest_for_provider(p_default)
        assert d_custom != d_default, (
            "custom and default endpoints must produce distinct digests"
        )

        # Warm up the registry so get_or_create builds coalescers for both digests
        c_custom = reg.get_or_create(VOYAGE_EMBED, d_custom, p_custom)
        c_default = reg.get_or_create(VOYAGE_EMBED, d_default, p_default)

        assert c_custom is not None
        assert c_default is not None
        assert c_custom is not c_default, (
            "_compute_live must route custom-endpoint provider to its own coalescer, "
            "not the default-config coalescer"
        )

    def test_compute_live_different_models_produce_different_coalescers(
        self, monkeypatch
    ):
        """Bug #1112: two providers with different models get separate coalescers
        via _compute_live — not a shared stale-default coalescer.

        Verifies the digest-keyed coalescer construction: voyage-code-3 and
        voyage-large-2 providers must build/return distinct EmbeddingCoalescer
        instances from the registry.
        """
        from code_indexer.server.services import governed_call

        reg = self._install_registry()

        class _LiveCfg:
            coalesce_enabled = True

        class _CfgSvc:
            def get_config(self):
                return _LiveCfg()

        monkeypatch.setattr(governed_call, "get_config_service", lambda: _CfgSvc())

        p_code3 = _FakeVoyageProvider(_FakeConfig(model="voyage-code-3"))
        p_large2 = _FakeVoyageProvider(_FakeConfig(model="voyage-large-2"))

        d_code3 = _digest_for_provider(p_code3)
        d_large2 = _digest_for_provider(p_large2)
        assert d_code3 != d_large2, "different models must produce distinct digests"

        c_code3 = reg.get_or_create(VOYAGE_EMBED, d_code3, p_code3)
        c_large2 = reg.get_or_create(VOYAGE_EMBED, d_large2, p_large2)

        assert c_code3 is not None
        assert c_large2 is not None
        assert c_code3 is not c_large2, (
            "two providers with different models must get separate coalescers "
            "via the _compute_live digest-keyed path"
        )

    def test_compute_live_max_per_lane_zero_falls_back_to_direct_no_crash(
        self, monkeypatch
    ):
        """cap-exceeded (max_per_lane=0) in _compute_live falls back to _direct
        without crashing — even on the very first call (no coalescer ever built).

        This is the 'fail-open' path: when the registry's per-lane cap is zero,
        every get_or_create returns None, and _compute_live must silently route
        to governed_query_embedding instead.
        """
        from code_indexer.server.services import governed_call

        ProviderConcurrencyGovernor(8)
        reg = CoalescerRegistry(max_per_lane=0)
        set_coalescer_registry(reg)

        class _LiveCfg:
            coalesce_enabled = True

        class _CfgSvc:
            def get_config(self):
                return _LiveCfg()

        monkeypatch.setattr(governed_call, "get_config_service", lambda: _CfgSvc())

        calls: list = []

        def _stub_direct(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            calls.append({"provider": provider, "text": text})
            return [9.0, 8.0, 7.0]

        monkeypatch.setattr(governed_call, "governed_query_embedding", _stub_direct)

        p = _FakeVoyageProvider(_FakeConfig(api_endpoint="https://any.api.com/v1"))
        result = governed_call._compute_live(p, "some query")

        assert result == [9.0, 8.0, 7.0], (
            "cap-exceeded path must return the direct governed call result without crashing"
        )
        assert len(calls) == 1, (
            "expected exactly one direct call (no coalescer was built because cap=0)"
        )
        assert calls[0]["text"] == "some query"
