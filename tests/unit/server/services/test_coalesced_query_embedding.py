"""Story #1079 Phase E — coalesced_query_embedding gating tests.

coalesced_query_embedding is the single entry point the 4 query sites call. Its
gating (server-gating via the registry + kill switch via runtime config) lives
ENTIRELY in this helper so the call sites are identical on CLI and server:

  1. No coalescer registry (CLI/solo — lifespan never built one)
        -> return governed_query_embedding(...) (direct governed single call).
  2. Registry present but coalesce_enabled is False (kill switch)
        -> return governed_query_embedding(...) (governor + AIMD still apply).
  3. Registry present, enabled, provider's :embed lane has a coalescer
        -> return coalescer.submit(text).
  4. Registry present, enabled, but that lane has NO coalescer (provider key
     absent) -> return governed_query_embedding(...) (explicit direct path).

CRITICAL acceptance criterion: in a CLI-like context (no registry), the helper
calls through to governed_query_embedding with NO coalescer constructed and no
batching wait. This guards "CLI paths untouched".

Story #1147 sub-task 3c adds TestCQEThinShim3c: end-to-end proof that after
the 3c rewire coalesced_query_embedding is a thin shim:
  - server path (coalescer present): _serve_with_cache NOT called; record_miss/
    record_hit each fire EXACTLY ONCE (no double-count from orphaned outer check).
  - shadow mode: exactly ONE record_shadow_cosine per key-resolution.
  - direct fallback (no coalescer): cache IS still consulted.
"""

import struct
import threading
from typing import Any, Dict, List, Optional, Tuple

import pytest

from code_indexer.server.services import governed_call
from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
    clear_coalescer_registry,
    set_coalescer_registry,
)
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

VOYAGE_EMBED = "voyage:embed"
COHERE_EMBED = "cohere:embed"
SENTINEL_VEC = [0.123, 0.456]
COALESCED_VEC = [9.0, 9.0]


class _FakeVoyageProvider:
    """Not a Cohere instance -> maps to voyage:embed."""


class _FakeCohereProvider:
    """Stand-in registered as the cohere isinstance target via patching."""


class _FakeCoalescer:
    def __init__(self) -> None:
        self.submitted: List[str] = []

    def submit(
        self,
        text: str,
        embedding_purpose: str = "query",
        *,
        no_embedding_cache_shortcut: bool = False,
        audit_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[float], EmbeddingCacheMetadata]:
        self.submitted.append(text)
        return COALESCED_VEC, EmbeddingCacheMetadata()


# ---------------------------------------------------------------------------
# Helpers for 3c gate tests (real cache + real coalescer, no mocks for cache I/O)
# ---------------------------------------------------------------------------

_3C_DIM = 3
_3C_LANE = "voyage:embed"
_3C_GOV_K = 8
_3C_PROVIDER_NAME = "voyage-ai"
_3C_MODEL = "voyage-code-3"
_3C_TEST_DIGEST = "cqe3c-e2e-testdigest"
LIVE_VEC_3C: List[float] = [1.0, 2.0, 3.0]
CACHED_VEC_3C: List[float] = [9.0, 8.0, 7.0]


def _enc_3c(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


class _FakeBackend3c:
    """Real in-memory dict backend (no DB I/O)."""

    def __init__(self) -> None:
        self._store: dict = {}

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


class _FakeVoyageProvider3c:
    """Fake provider that counts provider HTTP calls and returns a deterministic vec."""

    def __init__(self) -> None:
        self.call_count = 0
        self._lock = threading.Lock()

    def _count_tokens_accurately(self, text: str) -> int:
        return 1

    def _get_model_token_limit(self) -> int:
        return 120_000

    def get_provider_name(self) -> str:
        return _3C_PROVIDER_NAME

    def get_current_model(self) -> str:
        return _3C_MODEL

    def get_model_info(self) -> dict:
        return {"dimensions": _3C_DIM}

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        retry: bool = True,
        embedding_purpose: str = "document",
    ) -> List[List[float]]:
        with self._lock:
            self.call_count += 1
        return [[float(len(t) % 999), 0.0, 0.0] for t in texts]


def _make_real_cache_3c(
    mode: str = "on",
    pre_seed_text: Optional[str] = None,
    config_digest: str = _3C_TEST_DIGEST,
):
    """Real QueryEmbeddingCache backed by an in-memory fake backend."""
    from code_indexer.server.services.query_embedding_cache import (
        CacheQualifier,
        QueryEmbeddingCache,
        build_key,
    )

    backend = _FakeBackend3c()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode=mode, cohere_mode=mode
    )
    cache.mode_for = lambda pname: mode  # type: ignore[method-assign]
    qualifier = CacheQualifier(_3C_PROVIDER_NAME, _3C_MODEL, _3C_DIM)

    if pre_seed_text is not None:
        key = build_key(pre_seed_text, config_digest=config_digest)
        if key is not None:
            backend._store[(key, _3C_PROVIDER_NAME, _3C_MODEL, _3C_DIM)] = _enc_3c(
                CACHED_VEC_3C
            )

    return cache, qualifier, backend


def _wire_real_coalescer(monkeypatch, provider: _FakeVoyageProvider3c):
    """Build a real EmbeddingCoalescer and wire it into the registry."""
    gov = ProviderConcurrencyGovernor(max_concurrency=_3C_GOV_K)
    coalescer = EmbeddingCoalescer(
        _3C_LANE,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        config_digest=_3C_TEST_DIGEST,
    )

    class _FakeConfig3c:
        coalesce_enabled = True

    class _FakeConfigService3c:
        def get_config(self):
            return _FakeConfig3c()

    monkeypatch.setattr(
        governed_call,
        "get_config_service",
        lambda: _FakeConfigService3c(),
        raising=False,
    )

    reg = CoalescerRegistry.__new__(CoalescerRegistry)
    reg._coalescers = {_3C_LANE: coalescer}
    reg.get_or_create = lambda lane, digest, prov: coalescer
    set_coalescer_registry(reg)
    return coalescer, gov


class _FakeConfig:
    def __init__(self, coalesce_enabled: bool = True) -> None:
        self.coalesce_enabled = coalesce_enabled


class _FakeConfigService:
    def __init__(self, cfg: _FakeConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> _FakeConfig:
        return self._cfg


@pytest.fixture(autouse=True)
def _reset_registry():
    from code_indexer.server.services.config_service import reset_config_service

    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    reset_config_service()
    yield
    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    reset_config_service()


def _patch_governed(monkeypatch):
    """Replace governed_query_embedding with a spy returning SENTINEL_VEC."""
    calls = {}

    def _spy(provider, text, *, embedding_purpose="query", acquire_timeout=30.0):
        calls["provider"] = provider
        calls["text"] = text
        calls["embedding_purpose"] = embedding_purpose
        return SENTINEL_VEC

    monkeypatch.setattr(governed_call, "governed_query_embedding", _spy)
    return calls


def _patch_config(monkeypatch, cfg: _FakeConfig):
    monkeypatch.setattr(
        governed_call,
        "get_config_service",
        lambda: _FakeConfigService(cfg),
        raising=False,
    )


class TestNoRegistryDelegates:
    def test_cli_context_delegates_to_governed_single_call(self, monkeypatch):
        """No registry (CLI) -> direct governed single call, NO coalescer used."""
        calls = _patch_governed(monkeypatch)
        # No registry set, and config service must NOT even be needed.
        prov = _FakeVoyageProvider()
        out, _meta = governed_call.coalesced_query_embedding(prov, "hello")
        assert out == SENTINEL_VEC
        assert calls["provider"] is prov
        assert calls["text"] == "hello"


class TestKillSwitchDelegates:
    def test_coalesce_disabled_delegates(self, monkeypatch):
        """Registry present but coalesce_enabled False -> delegate."""
        _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=False))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(
            CoalescerRegistry.__new__(CoalescerRegistry)
        )  # placeholder; override get below
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: coalescer, raising=False)

        out, _meta = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), "hi"
        )
        assert out == SENTINEL_VEC  # delegated, not coalesced
        assert coalescer.submitted == []  # coalescer never used


class TestEnabledUsesCoalescer:
    def test_enabled_with_lane_uses_submit(self, monkeypatch):
        """Registry + enabled + lane present -> coalescer.submit().

        Intent preserved: when coalesce_enabled=True and the registry holds a
        coalescer for the lane, coalesced_query_embedding must call
        coalescer.submit() (not governed_query_embedding).

        Adaptation: _compute_live now calls registry.get_or_create(lane, digest,
        provider) instead of registry.get(lane), so we stub get_or_create to
        return the fake coalescer and capture the lane argument.
        """
        _patch_governed(monkeypatch)  # spy present but must NOT be called
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        captured: dict = {}

        def _get_or_create(lane, digest, provider):
            captured["lane"] = lane
            return coalescer

        monkeypatch.setattr(reg, "get_or_create", _get_or_create, raising=False)

        out, _meta = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), "abc"
        )
        assert out == COALESCED_VEC
        assert coalescer.submitted == ["abc"]
        assert captured["lane"] == VOYAGE_EMBED

    def test_cohere_provider_maps_to_cohere_embed_lane(self, monkeypatch):
        """A Cohere provider routes to the cohere:embed lane.

        Intent preserved: when the provider is a CohereEmbeddingProvider instance,
        _get_embedding_budget must return "cohere:embed" and the coalescer for
        that lane must be invoked.

        Adaptation: stub get_or_create (not get) to match the new dispatch path.
        """
        _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        captured: dict = {}

        def _get_or_create(lane, digest, provider):
            captured["lane"] = lane
            return coalescer

        monkeypatch.setattr(reg, "get_or_create", _get_or_create, raising=False)

        # Patch the cohere isinstance check used by _get_embedding_budget.
        from code_indexer.services import cohere_embedding

        monkeypatch.setattr(
            cohere_embedding, "CohereEmbeddingProvider", _FakeCohereProvider
        )
        out, _meta = governed_call.coalesced_query_embedding(_FakeCohereProvider(), "z")
        assert out == COALESCED_VEC
        assert captured["lane"] == COHERE_EMBED


class TestHotReload:
    def test_toggling_coalesce_enabled_takes_effect_without_restart(self, monkeypatch):
        """Flipping coalesce_enabled live switches behavior between calls.

        Intent preserved: when coalesce_enabled is False, coalesced_query_embedding
        delegates to governed_query_embedding (no coalescer used). After flipping
        coalesce_enabled=True on the live config object (no restart, no re-registration),
        the next call must use the coalescer.

        Adaptation: stub get_or_create (not get) to match the new _compute_live
        dispatch path. The behavioral contract is identical.
        """
        calls = _patch_governed(monkeypatch)
        cfg = _FakeConfig(coalesce_enabled=False)
        _patch_config(monkeypatch, cfg)
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(
            reg,
            "get_or_create",
            lambda lane, digest, provider: coalescer,
            raising=False,
        )

        prov = _FakeVoyageProvider()
        # First call: disabled -> delegates.
        out1, _meta1 = governed_call.coalesced_query_embedding(prov, "first")
        assert out1 == SENTINEL_VEC
        assert coalescer.submitted == []
        assert calls["text"] == "first"

        # Flip the LIVE config; no restart, no re-registration.
        cfg.coalesce_enabled = True
        out2, _meta2 = governed_call.coalesced_query_embedding(prov, "second")
        assert out2 == COALESCED_VEC
        assert coalescer.submitted == ["second"]


class TestEnabledButLaneAbsentDelegates:
    def test_absent_lane_delegates(self, monkeypatch):
        """Registry + enabled but no coalescer for the lane -> delegate."""
        calls = _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: None, raising=False)

        out, _meta = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "q")
        assert out == SENTINEL_VEC
        assert calls["text"] == "q"


class TestConfigReadFailureDelegates:
    def test_unreadable_config_delegates(self, monkeypatch):
        """Registry present but config read raises -> defensive direct call."""
        calls = _patch_governed(monkeypatch)
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: coalescer, raising=False)

        def _boom():
            raise RuntimeError("config service not initialized")

        monkeypatch.setattr(governed_call, "get_config_service", _boom, raising=False)

        out, _meta = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "x")
        assert out == SENTINEL_VEC  # delegated (fail toward direct call)
        assert coalescer.submitted == []  # coalescer never used
        assert calls["text"] == "x"


# ===========================================================================
# Story #1147 sub-task 3c: end-to-end gate tests
#
# Drive the REAL coalesced_query_embedding() entry point (not coalescer.submit
# directly) and assert single-count hit/miss semantics with no double-check
# from a leftover _serve_with_cache call on the server path.
# ===========================================================================


@pytest.fixture(autouse=False)
def _reset_singletons_3c():
    """Reset governor, health monitor, cache accessors, registry, and config service
    between 3c tests.  The config service singleton is reset so that the real
    QueryEmbeddingCache.enabled_for() / _live_qec_cfg() path sees a clean config
    and is not polluted by ConfigService instances created by earlier tests."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
    from code_indexer.server.services.config_service import reset_config_service

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    clear_coalescer_registry()
    reset_config_service()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    clear_coalescer_registry()
    reset_config_service()


class TestCQEThinShim3c:
    """Story #1147 3c — coalesced_query_embedding is a thin shim gate tests.

    All tests drive the REAL entry point coalesced_query_embedding(), NOT
    coalescer.submit() directly. Uses real EmbeddingCoalescer, real
    QueryEmbeddingCache (in-memory backend), and real ProviderConcurrencyGovernor.
    The only thing spied is _serve_with_cache (to prove it is NOT on the hot
    path when a coalescer is wired) and cache record methods (to count calls).
    """

    # ------------------------------------------------------------------
    # Gate 1: _serve_with_cache must NOT be called when coalescer is wired
    # ------------------------------------------------------------------

    def test_serve_with_cache_not_called_on_server_path(
        self, monkeypatch, _reset_singletons_3c
    ):
        """_serve_with_cache must NOT be called when a coalescer is wired.

        This is the primary proof that the orphaned double-cache-check is gone.
        We spy on _serve_with_cache and assert it is never called when the
        coalescer registry is installed (coalescer.submit() owns cache).
        """
        text = "server path orphan check 3c"
        cache, _, _ = _make_real_cache_3c(mode="on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        provider = _FakeVoyageProvider3c()
        coalescer, _ = _wire_real_coalescer(monkeypatch, provider)

        serve_with_cache_calls = [0]
        original_swc = governed_call._serve_with_cache

        def spy_swc(*args, **kwargs):
            serve_with_cache_calls[0] += 1
            return original_swc(*args, **kwargs)

        monkeypatch.setattr(governed_call, "_serve_with_cache", spy_swc)

        # MISS path (cache empty)
        governed_call.coalesced_query_embedding(provider, text)

        assert serve_with_cache_calls[0] == 0, (
            f"_serve_with_cache must NOT be called when coalescer is present "
            f"(got {serve_with_cache_calls[0]} calls). "
            "The coalescer.submit() owns cache lookup; _serve_with_cache is "
            "only for the direct fallback path (no coalescer)."
        )

    # ------------------------------------------------------------------
    # Gate 2: record_miss fires EXACTLY ONCE on miss (no double-count)
    # ------------------------------------------------------------------

    def test_on_mode_miss_record_miss_fires_exactly_once(
        self, monkeypatch, _reset_singletons_3c
    ):
        """On-mode MISS: record_miss_or_shadow fires EXACTLY ONCE.

        Before 3c: _serve_with_cache fires once THEN coalescer writes again
        after live embed -> double-count. After 3c: only the coalescer writes.
        """
        cache, _, _ = _make_real_cache_3c(mode="on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        miss_calls = [0]
        original = cache.record_miss_or_shadow

        def track_miss(*args, **kwargs):
            miss_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_miss_or_shadow = track_miss  # type: ignore[method-assign]

        provider = _FakeVoyageProvider3c()
        _wire_real_coalescer(monkeypatch, provider)

        governed_call.coalesced_query_embedding(provider, "unique miss text 3c-abc")

        assert miss_calls[0] == 1, (
            f"record_miss_or_shadow must fire EXACTLY ONCE on a MISS, "
            f"got {miss_calls[0]}. "
            "Double-count means _serve_with_cache AND coalescer are both writing."
        )
        assert provider.call_count == 1, "Provider must be called exactly once on MISS"

    # ------------------------------------------------------------------
    # Gate 3: record_hit EXACTLY ONCE on repeat; zero provider calls
    # ------------------------------------------------------------------

    def test_on_mode_hit_record_hit_exactly_once_zero_provider_calls(
        self, monkeypatch, _reset_singletons_3c
    ):
        """On-mode HIT: record_hit fires EXACTLY ONCE; zero provider calls on repeat.

        Flow: MISS (first) -> vector cached. HIT (second) -> record_hit exactly
        once (not twice from double-check). Provider must NOT be called on second.
        """
        cache, _, _ = _make_real_cache_3c(mode="on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        hit_calls = [0]
        original_hit = cache.record_hit

        def track_hit(*args, **kwargs):
            hit_calls[0] += 1
            return original_hit(*args, **kwargs)

        cache.record_hit = track_hit  # type: ignore[method-assign]

        provider = _FakeVoyageProvider3c()
        _wire_real_coalescer(monkeypatch, provider)

        text = "repeated query for 3c hit test"

        # First: MISS — embed live, write to cache
        governed_call.coalesced_query_embedding(provider, text)
        assert provider.call_count == 1, "First call must go live (MISS)"
        assert hit_calls[0] == 0, "No record_hit on first call (it is a MISS)"

        # Reset counter, then test the HIT
        hit_calls[0] = 0
        governed_call.coalesced_query_embedding(provider, text)

        assert provider.call_count == 1, (
            f"Second call must be a cache HIT (zero additional provider calls), "
            f"got call_count={provider.call_count}"
        )
        assert hit_calls[0] == 1, (
            f"record_hit must fire EXACTLY ONCE on HIT (not double-count), "
            f"got {hit_calls[0]}. "
            "Double-count means _serve_with_cache AND coalescer are both recording."
        )

    # ------------------------------------------------------------------
    # Gate 4: shadow mode — exactly ONE record_shadow_cosine per key-resolution
    # ------------------------------------------------------------------

    def test_shadow_mode_exactly_one_record_shadow_cosine(
        self, monkeypatch, _reset_singletons_3c
    ):
        """Shadow mode: exactly ONE record_shadow_cosine; live vector served.

        Shadow asymmetry (Story #1147 spec): record_shadow_cosine fires ONCE per
        key-resolution inside the coalescer (not once per _serve_with_cache call).
        """
        text = "shadow cosine 3c test query"
        cache, _, _ = _make_real_cache_3c(mode="shadow", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        shadow_cosine_calls = [0]

        class _SpyMetrics:
            def record_hit(self, *, mode: str, provider: str) -> None:
                pass

            def record_miss(self, *, mode: str, provider: str) -> None:
                pass

            def record_shadow_cosine(
                self, *, cached_blob: bytes, live_vec: List[float]
            ) -> None:
                shadow_cosine_calls[0] += 1

            def record_long_key(self, *, provider: str) -> None:
                pass

        governed_call.set_query_embedding_cache_metrics(_SpyMetrics())

        provider = _FakeVoyageProvider3c()
        _wire_real_coalescer(monkeypatch, provider)

        result, _meta = governed_call.coalesced_query_embedding(provider, text)

        # Shadow: always embeds live
        assert provider.call_count == 1, (
            "Shadow mode must always call provider (one call per key-resolution)"
        )
        # Shadow: live vector served (not the pre-seeded cached vec)
        expected_live = [float(len(text) % 999), 0.0, 0.0]
        assert result == pytest.approx(expected_live, abs=1e-4), (
            f"Shadow mode must serve live vector, got {result}"
        )
        # Exactly ONE record_shadow_cosine per key-resolution (not double from
        # both _serve_with_cache and coalescer's post-dispatch write)
        assert shadow_cosine_calls[0] == 1, (
            f"record_shadow_cosine must fire EXACTLY ONCE per key-resolution, "
            f"got {shadow_cosine_calls[0]}. "
            "Double-count means _serve_with_cache AND coalescer are both recording."
        )

    # ------------------------------------------------------------------
    # Gate 5a: direct fallback (no coalescer) — cache still writes on MISS
    # ------------------------------------------------------------------

    def test_direct_fallback_no_coalescer_cache_writes_on_miss(
        self, monkeypatch, _reset_singletons_3c
    ):
        """Direct fallback (no coalescer): cache IS still consulted on MISS.

        Covers: CLI/solo, kill-switch off, concurrency cap exceeded.
        The direct path must write to cache on miss so future on-mode requests HIT.
        """
        clear_coalescer_registry()

        cache, _, _ = _make_real_cache_3c(mode="on")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        miss_calls = [0]
        original = cache.record_miss_or_shadow

        def track_miss(*args, **kwargs):
            miss_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_miss_or_shadow = track_miss  # type: ignore[method-assign]

        governed_calls = [0]

        def _fake_governed(prov, txt, *, embedding_purpose=None, acquire_timeout=30.0):
            governed_calls[0] += 1
            return LIVE_VEC_3C

        monkeypatch.setattr(
            governed_call, "governed_query_embedding", _fake_governed, raising=False
        )

        class _MinimalProvider:
            def get_provider_name(self):
                return _3C_PROVIDER_NAME

            def get_current_model(self):
                return _3C_MODEL

            def get_model_info(self):
                return {"dimensions": _3C_DIM}

        result, _meta = governed_call.coalesced_query_embedding(
            _MinimalProvider(), "direct fallback 3c no coalescer"
        )

        assert result == LIVE_VEC_3C
        assert governed_calls[0] == 1, (
            "Direct fallback must call governed_query_embedding"
        )
        assert miss_calls[0] == 1, (
            f"Direct fallback must write to cache on MISS, "
            f"got {miss_calls[0]} record_miss_or_shadow calls"
        )

    # ------------------------------------------------------------------
    # Gate 5b: direct fallback (no coalescer) — HIT skips provider
    # ------------------------------------------------------------------

    def test_direct_fallback_no_coalescer_hit_skips_provider(
        self, monkeypatch, _reset_singletons_3c
    ):
        """Direct fallback (no coalescer) on-mode HIT: governed_query_embedding skipped.

        Pre-seed with the actual fallback digest (_FALLBACK_DIGEST) so the key
        matches what the direct-fallback path computes via _digest_for_provider.
        """
        from code_indexer.server.services.coalescer_registry import _FALLBACK_DIGEST
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            build_key,
        )

        clear_coalescer_registry()

        text = "direct fallback 3c hit check"
        backend = _FakeBackend3c()
        cache = QueryEmbeddingCache(
            backend, enabled=True, voyage_mode="on", cohere_mode="on"
        )
        cache.mode_for = lambda pname: "on"  # type: ignore[method-assign]

        # Pre-seed using the actual digest the direct-fallback path computes
        direct_key = build_key(text, config_digest=_FALLBACK_DIGEST)
        assert direct_key is not None
        backend._store[(direct_key, _3C_PROVIDER_NAME, _3C_MODEL, _3C_DIM)] = _enc_3c(
            CACHED_VEC_3C
        )

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        governed_calls = [0]

        def _fake_governed(prov, txt, *, embedding_purpose=None, acquire_timeout=30.0):
            governed_calls[0] += 1
            return LIVE_VEC_3C

        monkeypatch.setattr(
            governed_call, "governed_query_embedding", _fake_governed, raising=False
        )

        class _MinimalProvider:
            def get_provider_name(self):
                return _3C_PROVIDER_NAME

            def get_current_model(self):
                return _3C_MODEL

            def get_model_info(self):
                return {"dimensions": _3C_DIM}

        result, _meta = governed_call.coalesced_query_embedding(
            _MinimalProvider(), text
        )

        assert governed_calls[0] == 0, (
            f"Direct fallback on-mode HIT must skip governed_query_embedding, "
            f"got {governed_calls[0]} calls"
        )
        assert result == pytest.approx(CACHED_VEC_3C, abs=1e-4), (
            f"Direct fallback HIT must return cached vec, got {result}"
        )
