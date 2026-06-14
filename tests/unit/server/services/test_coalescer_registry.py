"""Story #1079 Phase E — coalescer registry tests.

The registry holds ONE EmbeddingCoalescer per configured ``:embed`` lane and is
constructed ONLY in server lifespan. A process-level accessor returns None until
lifespan sets it (CLI/solo never builds one), which is exactly how
coalesced_query_embedding gates: no registry -> direct governed single call.

Anti-mock: the registry wraps REAL EmbeddingCoalescer instances over scripted
fake providers shaped like the real Voyage/Cohere embedding providers.
"""

from typing import List

import pytest

from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
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


class _FakeVoyageProvider:
    def __init__(self, token_limit: int = 120000) -> None:
        self._token_limit = token_limit

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


class _FakeCohereProvider(_FakeVoyageProvider):
    _count_tokens_accurately = None  # type: ignore[assignment]

    def _count_tokens(self, text: str) -> int:
        return 1

    def _get_texts_per_request(self) -> int:
        return 96


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


class TestProcessAccessor:
    def test_none_until_set(self):
        assert get_coalescer_registry() is None

    def test_set_then_get(self):
        reg = CoalescerRegistry(coalescers={})
        set_coalescer_registry(reg)
        assert get_coalescer_registry() is reg

    def test_clear_resets_to_none(self):
        set_coalescer_registry(CoalescerRegistry(coalescers={}))
        clear_coalescer_registry()
        assert get_coalescer_registry() is None


class TestRegistryLookup:
    def test_get_returns_lane_coalescer(self):
        gov = ProviderConcurrencyGovernor(8)
        voyage_c = EmbeddingCoalescer(VOYAGE_EMBED, _FakeVoyageProvider(), governor=gov)
        reg = CoalescerRegistry(coalescers={VOYAGE_EMBED: voyage_c})
        assert reg.get(VOYAGE_EMBED) is voyage_c

    def test_get_absent_lane_returns_none(self):
        gov = ProviderConcurrencyGovernor(8)
        voyage_c = EmbeddingCoalescer(VOYAGE_EMBED, _FakeVoyageProvider(), governor=gov)
        reg = CoalescerRegistry(coalescers={VOYAGE_EMBED: voyage_c})
        # Cohere lane absent (key not configured) -> None (caller falls back).
        assert reg.get(COHERE_EMBED) is None

    def test_two_lane_registry(self):
        gov = ProviderConcurrencyGovernor(8)
        v = EmbeddingCoalescer(VOYAGE_EMBED, _FakeVoyageProvider(), governor=gov)
        c = EmbeddingCoalescer(COHERE_EMBED, _FakeCohereProvider(), governor=gov)
        reg = CoalescerRegistry(coalescers={VOYAGE_EMBED: v, COHERE_EMBED: c})
        assert reg.get(VOYAGE_EMBED) is v
        assert reg.get(COHERE_EMBED) is c


class TestRegistryCoalescesRealSubmits:
    def test_submit_through_registry_coalescer_returns_vector(self):
        gov = ProviderConcurrencyGovernor(8)
        v = EmbeddingCoalescer(VOYAGE_EMBED, _FakeVoyageProvider(), governor=gov)
        reg = CoalescerRegistry(coalescers={VOYAGE_EMBED: v})
        coalescer = reg.get(VOYAGE_EMBED)
        assert coalescer is not None
        vec = coalescer.submit("abcd")
        assert vec == [4.0, 0.0]


GOVERNOR_K = 8
DEFAULT_MAX_BATCH_SIZE = 96
LOWER_CEILING = 32


class _Cfg:
    def __init__(self, coalesce_max_batch_size: int = DEFAULT_MAX_BATCH_SIZE) -> None:
        self.coalesce_max_batch_size = coalesce_max_batch_size


def _missing_key_builder(_http_client_factory):
    """Stand-in for a provider whose API-key env var is absent (constructor raises)."""
    raise RuntimeError("API key not configured")


def _patch_builders(monkeypatch, lane_to_provider):
    """Patch the registry's _LANE_PROVIDER_BUILDERS lane->constructor map.

    A lane present in ``lane_to_provider`` builds that fake provider; a lane
    absent from it gets a builder that raises (simulating a missing API key).
    """
    from code_indexer.server.services import coalescer_registry

    builders: dict = {}
    for lane in (VOYAGE_EMBED, COHERE_EMBED):
        provider = lane_to_provider.get(lane)
        if provider is None:
            builders[lane] = _missing_key_builder
        else:
            builders[lane] = lambda _hcf, _p=provider: _p
    monkeypatch.setattr(coalescer_registry, "_LANE_PROVIDER_BUILDERS", builders)


class TestBuildRegistry:
    def test_voyage_only_builds_voyage_lane_only(self, monkeypatch):
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)  # ensure singleton exists
        _patch_builders(monkeypatch, {VOYAGE_EMBED: _FakeVoyageProvider()})
        reg = build_coalescer_registry(_Cfg())
        assert reg.get(VOYAGE_EMBED) is not None
        assert reg.get(COHERE_EMBED) is None

    def test_both_providers_build_both_lanes_with_ceiling(self, monkeypatch):
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)
        _patch_builders(
            monkeypatch,
            {
                VOYAGE_EMBED: _FakeVoyageProvider(),
                COHERE_EMBED: _FakeCohereProvider(),
            },
        )
        reg = build_coalescer_registry(_Cfg(coalesce_max_batch_size=LOWER_CEILING))
        assert reg.get(VOYAGE_EMBED) is not None
        cohere_c = reg.get(COHERE_EMBED)
        assert cohere_c is not None
        # Cohere fake caps texts at 96; the lower config ceiling wins (min).
        assert cohere_c.texts_cap == LOWER_CEILING

    def test_no_providers_builds_empty_registry(self, monkeypatch):
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)
        _patch_builders(monkeypatch, {})
        reg = build_coalescer_registry(_Cfg())
        assert reg.get(VOYAGE_EMBED) is None
        assert reg.get(COHERE_EMBED) is None

    def test_built_coalescer_has_live_ceiling_provider(self, monkeypatch):
        """The built coalescer's effective cap follows the LIVE config ceiling."""
        from code_indexer.server.services import coalescer_registry
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)
        _patch_builders(monkeypatch, {VOYAGE_EMBED: _FakeVoyageProvider()})

        # The builder reads coalesce_max_batch_size LIVE from get_config_service().
        live_cfg = _Cfg(coalesce_max_batch_size=DEFAULT_MAX_BATCH_SIZE)

        class _Svc:
            def get_config(self):
                return live_cfg

        monkeypatch.setattr(
            coalescer_registry, "get_config_service", lambda: _Svc(), raising=False
        )

        reg = build_coalescer_registry(live_cfg)
        coalescer = reg.get(VOYAGE_EMBED)
        assert coalescer is not None
        assert coalescer.effective_texts_cap() == DEFAULT_MAX_BATCH_SIZE
        # Hot-reload: change the live config; effective cap follows, no rebuild.
        live_cfg.coalesce_max_batch_size = LOWER_CEILING
        assert coalescer.effective_texts_cap() == LOWER_CEILING
