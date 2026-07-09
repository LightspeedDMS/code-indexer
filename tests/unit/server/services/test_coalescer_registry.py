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

    def get_provider_name(self) -> str:
        """Real VoyageAIClient implements this; the coalescer's _dispatch()
        reads it to attribute emitted events (Story #1293)."""
        return "voyage-ai"

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

    def get_provider_name(self) -> str:
        return "cohere"


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
        vec, _ = coalescer.submit("abcd")
        assert vec == [4.0, 0.0]


GOVERNOR_K = 8
DEFAULT_MAX_BATCH_SIZE = 96
LOWER_CEILING = 32


class _Cfg:
    def __init__(self, coalesce_max_batch_size: int = DEFAULT_MAX_BATCH_SIZE) -> None:
        self.coalesce_max_batch_size = coalesce_max_batch_size


class TestBuildRegistry:
    """Tests for build_coalescer_registry and lazy CoalescerRegistry semantics.

    Bug #1112 replaced the OLD eager-build (one provider per lane at startup via
    _LANE_PROVIDER_BUILDERS) with a LAZY digest-keyed design.  These tests assert
    the new behavior: build_coalescer_registry returns an EMPTY registry (no
    providers constructed at build time); get_or_create lazily builds a coalescer
    from the CALLER's provider.

    Intent mapping from old -> new:
      test_voyage_only_builds_voyage_lane_only   -> lazy get_or_create on voyage lane
                                                    builds a coalescer; cohere stays None
      test_both_providers_build_both_lanes_with_ceiling -> lazy get_or_create on both
                                                    lanes; ceiling respected
      test_no_providers_builds_empty_registry    -> build_coalescer_registry returns
                                                    empty registry (get returns None)
      test_built_coalescer_has_live_ceiling_provider -> lazily-built coalescer's
                                                    effective cap follows live ceiling
    """

    def test_voyage_only_builds_voyage_lane_only(self, monkeypatch):
        """Lazy: get_or_create on voyage lane builds a coalescer; cohere stays None.

        Old intent: only voyage lane is populated (cohere absent).
        New invariant: the registry starts EMPTY; get_or_create on voyage builds
        a coalescer; a cohere get_or_create call is never made -> cohere stays None.
        """
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
            _digest_for_provider,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)
        reg = build_coalescer_registry(_Cfg())

        # Before any get_or_create: both lanes are empty.
        assert reg.get(VOYAGE_EMBED) is None
        assert reg.get(COHERE_EMBED) is None

        # Lazily build a voyage coalescer via get_or_create.
        voyage_prov = _FakeVoyageProvider()
        digest = _digest_for_provider(voyage_prov)
        coalescer = reg.get_or_create(VOYAGE_EMBED, digest, voyage_prov)
        assert coalescer is not None

        # Cohere lane was never touched -> still None.
        assert reg.get(COHERE_EMBED) is None

    def test_both_providers_build_both_lanes_with_ceiling(self, monkeypatch):
        """Lazy: get_or_create on both lanes builds coalescers; ceiling is respected.

        Old intent: both lanes populated; lower config ceiling wins over provider cap.
        New invariant: both coalescers are built lazily from the caller's provider;
        the coalescer built with the lower-ceiling registry respects that ceiling.
        """
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
            _digest_for_provider,
        )
        from code_indexer.server.services import coalescer_registry

        ProviderConcurrencyGovernor(GOVERNOR_K)

        live_cfg = _Cfg(coalesce_max_batch_size=LOWER_CEILING)

        class _Svc:
            def get_config(self):
                return live_cfg

        monkeypatch.setattr(
            coalescer_registry, "get_config_service", lambda: _Svc(), raising=False
        )

        reg = build_coalescer_registry(live_cfg)

        voyage_prov = _FakeVoyageProvider()
        cohere_prov = _FakeCohereProvider()

        v_digest = _digest_for_provider(voyage_prov)
        c_digest = _digest_for_provider(cohere_prov)

        voyage_c = reg.get_or_create(VOYAGE_EMBED, v_digest, voyage_prov)
        cohere_c = reg.get_or_create(COHERE_EMBED, c_digest, cohere_prov)

        assert voyage_c is not None
        assert cohere_c is not None

        # The live ceiling (LOWER_CEILING=32) governs the coalescer's effective cap.
        assert cohere_c.effective_texts_cap() == LOWER_CEILING

    def test_no_providers_builds_empty_registry(self, monkeypatch):
        """build_coalescer_registry returns an empty registry (no pre-built coalescers).

        Old intent: no providers -> empty registry.
        New invariant: build_coalescer_registry ALWAYS returns empty; get(lane)
        returns None for any lane immediately after build.
        """
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)
        reg = build_coalescer_registry(_Cfg())

        # Immediately after build: no coalescers have been lazily constructed yet.
        assert reg.get(VOYAGE_EMBED) is None
        assert reg.get(COHERE_EMBED) is None

    def test_built_coalescer_has_live_ceiling_provider(self, monkeypatch):
        """The lazily-built coalescer's effective cap follows the LIVE config ceiling.

        Old intent: built coalescer hot-reloads ceiling from live config.
        New invariant: SAME — the lazily-built coalescer receives the same
        ceiling_provider closure that reads coalesce_max_batch_size live, so a
        runtime change to the config takes effect without a registry rebuild.
        """
        from code_indexer.server.services import coalescer_registry
        from code_indexer.server.services.coalescer_registry import (
            build_coalescer_registry,
            _digest_for_provider,
        )

        ProviderConcurrencyGovernor(GOVERNOR_K)

        live_cfg = _Cfg(coalesce_max_batch_size=DEFAULT_MAX_BATCH_SIZE)

        class _Svc:
            def get_config(self):
                return live_cfg

        monkeypatch.setattr(
            coalescer_registry, "get_config_service", lambda: _Svc(), raising=False
        )

        reg = build_coalescer_registry(live_cfg)

        # Lazily build the coalescer via get_or_create.
        voyage_prov = _FakeVoyageProvider()
        digest = _digest_for_provider(voyage_prov)
        coalescer = reg.get_or_create(VOYAGE_EMBED, digest, voyage_prov)
        assert coalescer is not None
        assert coalescer.effective_texts_cap() == DEFAULT_MAX_BATCH_SIZE

        # Hot-reload: change the live config; effective cap follows, no rebuild.
        live_cfg.coalesce_max_batch_size = LOWER_CEILING
        assert coalescer.effective_texts_cap() == LOWER_CEILING
