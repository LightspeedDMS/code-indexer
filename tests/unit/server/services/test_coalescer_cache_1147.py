"""Story #1147 — Relocate mode-aware cache + normalization into EmbeddingCoalescer.

Organized by sub-task:
  3a: Accessor-at-submit wiring (cache not injected via constructor; CLI None path)
  3b: Lock-free pre-enqueue cache check (on-hit no-enqueue/no-slot, on-miss embeds, counters,
      shadow always-embed, off/disabled/CLI direct)
  3c: Thin-shim coalesced_query_embedding + direct-fallback-still-cache-checks
  3d: _Entry gains audit_ctx + no_embedding_cache_shortcut slots; per-requestor bypass

Design invariants (from Story #1147 spec):
  - Cache I/O NEVER runs under self._lock (lock-free check BEFORE _enqueue).
  - on-mode HIT: zero governor slots consumed, zero provider calls, return immediately.
  - shadow-mode: ALWAYS embed live, ONE record_shadow_cosine per key-resolution.
  - off/disabled/CLI: direct path, no cache.
  - bypass (no_embedding_cache_shortcut=True): skip READ, still WRITE; bypass joins
    only in-flight LIVE resolves, never cached-hit resolves.
  - _Entry.__slots__ must include audit_ctx and no_embedding_cache_shortcut.
  - The #1110 deep-fidelity audit stays at FSV chokepoint (NOT moved here).
  - _serve_with_cache stays as helper for the direct path (non-coalescer case).
"""

from __future__ import annotations

import inspect
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.embedding_coalescer import (
    EmbeddingCoalescer,
    _Entry,
)
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LANE = "voyage:embed"
GOV_K = 8
_TEST_DIGEST = "abc123testdigest1147"
PROVIDER_NAME = "voyage-ai"
MODEL = "voyage-code-3"
DIM = 3

LIVE_VEC: List[float] = [1.0, 2.0, 3.0]
CACHED_VEC: List[float] = [9.0, 8.0, 7.0]

# Path to embedding_coalescer.py derived from module __file__
import code_indexer.server.services.embedding_coalescer as _coalescer_mod  # noqa: E402

_COALESCER_PATH = Path(_coalescer_mod.__file__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enc(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _dec(blob: bytes) -> List[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ---------------------------------------------------------------------------
# Fake provider (Voyage-shaped, no real HTTP)
# ---------------------------------------------------------------------------


class _FakeVoyageProvider:
    """Deterministic fake provider that counts calls."""

    def __init__(self, token_limit: int = 120_000, tokens_per_text: int = 1) -> None:
        self._token_limit = token_limit
        self._tokens_per_text = tokens_per_text
        self.call_count = 0
        self.calls_texts: List[List[str]] = []
        self._lock = threading.Lock()

    def _count_tokens_accurately(self, text: str) -> int:
        return self._tokens_per_text

    def _get_model_token_limit(self) -> int:
        return self._token_limit

    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_current_model(self) -> str:
        return MODEL

    def get_model_info(self) -> dict:
        return {"dimensions": DIM}

    def get_embeddings_batch(
        self,
        texts: List[str],
        *,
        retry: bool = True,
        embedding_purpose: str = "document",
    ) -> List[List[float]]:
        with self._lock:
            self.call_count += 1
            self.calls_texts.append(list(texts))
        # Return a deterministic per-text vector (length-based)
        return [[float(len(t) % 999), 0.0, 0.0] for t in texts]


# ---------------------------------------------------------------------------
# Fake in-memory cache backend (real dict, no DB)
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self._store: dict = {}
        self._count = 0

    def lookup(self, key, provider, model, dimension) -> Optional[bytes]:
        return self._store.get((key, provider, model, dimension))

    def upsert(self, key, provider, model, dimension, blob, last_used, created_at):
        self._store[(key, provider, model, dimension)] = blob
        self._count = len(self._store)

    def touch_last_used(self, key, provider, model, dimension, ts):
        pass

    def prune_to_max(self, max_entries):
        pass

    def total_entries(self) -> int:
        return self._count


def _make_real_cache(mode: str = "on", pre_seed_text: Optional[str] = None):
    """Build a QueryEmbeddingCache with a real in-memory backend (no mocks)."""
    from code_indexer.server.services.query_embedding_cache import (
        CacheQualifier,
        QueryEmbeddingCache,
        build_key,
    )

    backend = _FakeBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode=mode, cohere_mode=mode
    )
    # Pin mode so tests are deterministic (bypass config service)
    cache.mode_for = lambda pname: mode  # type: ignore[method-assign]

    qualifier = CacheQualifier(PROVIDER_NAME, MODEL, DIM)

    if pre_seed_text is not None:
        key = build_key(pre_seed_text, config_digest=_TEST_DIGEST)
        if key is not None:
            backend._store[(key, PROVIDER_NAME, MODEL, DIM)] = _enc(CACHED_VEC)
            backend._count = 1

    return cache, qualifier


# ---------------------------------------------------------------------------
# Fixture: reset singletons
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
    from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
    from code_indexer.server.services.config_service import reset_config_service

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    # Clear governed_call process-level cache
    from code_indexer.server.services import governed_call

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


# ---------------------------------------------------------------------------
# Saturation harness (same pattern as test_embedding_coalescer_dedup_1146.py)
# ---------------------------------------------------------------------------


def _saturate(
    governor: ProviderConcurrencyGovernor, lane: str, hold: threading.Event
) -> List[threading.Thread]:
    bar = threading.Barrier(GOV_K + 1)
    threads: List[threading.Thread] = []

    def _blocker() -> None:
        def _h() -> str:
            bar.wait()
            hold.wait(timeout=30)
            return "ok"

        governor.execute(lane, _h, acquire_timeout=30.0)

    for _ in range(GOV_K):
        t = threading.Thread(target=_blocker, daemon=True)
        t.start()
        threads.append(t)
    bar.wait()
    return threads


class _Outcome:
    def __init__(self) -> None:
        self.results: Dict[int, List[float]] = {}
        self.errors: Dict[int, BaseException] = {}


def _run_saturated_submits(
    coalescer: EmbeddingCoalescer,
    governor: ProviderConcurrencyGovernor,
    lane: str,
    texts: List[str],
    *,
    accumulate: float = 0.3,
    extra_kwargs: Optional[dict] = None,
) -> _Outcome:
    import time

    hold = threading.Event()
    blockers = _saturate(governor, lane, hold)
    outcome = _Outcome()
    n = len(texts)
    start = threading.Barrier(n)

    kw = extra_kwargs or {}

    def _submit(i: int) -> None:
        start.wait()
        try:
            outcome.results[i] = coalescer.submit(texts[i], **kw)
        except BaseException as ex:  # noqa: BLE001
            outcome.errors[i] = ex

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(_submit, i) for i in range(n)]
        time.sleep(accumulate)
        hold.set()
        for f in futs:
            f.result(timeout=30)

    hold.set()
    for t in blockers:
        t.join(timeout=5)
    return outcome


# ===========================================================================
# 3a: Accessor-at-submit wiring
# ===========================================================================


class TestAccessorAtSubmit:
    """3a: EmbeddingCoalescer must NOT accept cache in constructor;
    must call get_query_embedding_cache() at submit time."""

    def test_constructor_has_no_cache_parameter(self):
        """EmbeddingCoalescer.__init__ must NOT have a 'cache' parameter."""
        sig = inspect.signature(EmbeddingCoalescer.__init__)
        assert "cache" not in sig.parameters, (
            "EmbeddingCoalescer constructor must NOT accept 'cache' — "
            "it must use get_query_embedding_cache() accessor at submit time"
        )

    def test_constructor_has_no_metrics_parameter(self):
        """EmbeddingCoalescer.__init__ must NOT have a 'metrics' parameter."""
        sig = inspect.signature(EmbeddingCoalescer.__init__)
        assert "metrics" not in sig.parameters, (
            "EmbeddingCoalescer constructor must NOT accept 'metrics' — "
            "it must use get_query_embedding_cache_metrics() accessor at submit time"
        )

    def test_cli_none_accessor_uses_direct_path_no_cache_ops(self, monkeypatch):
        """When get_query_embedding_cache() returns None (CLI path), submit goes
        live directly and performs NO cache operations."""
        from code_indexer.server.services import governed_call

        # Ensure accessor returns None
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        result = coalescer.submit("hello world")
        # Provider was called (live path)
        assert provider.call_count == 1
        # Result is the live vector
        assert isinstance(result, list) and len(result) > 0

    def test_accessor_called_at_submit_time_not_construction(self, monkeypatch):
        """Cache accessor is called at submit time, NOT at construction time.

        Install a cache AFTER the coalescer is constructed — it must still be
        used (because the lookup happens at submit(), not __init__()).
        """
        from code_indexer.server.services import governed_call

        # First, no cache
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )
        # At construction: no cache => no cache instance held

        # NOW install a real cache with on-mode (EMPTY — no pre-seeded entry)
        # The point is: the cache was installed AFTER construction, so if the
        # coalescer used constructor injection it would have gotten None. With
        # accessor-at-submit, it picks up the newly installed cache.
        cache, _ = _make_real_cache(mode="on")  # empty, no pre-seed
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        # Submit — since cache was installed AFTER construction, the accessor-at-submit
        # pattern must find it now and attempt a lookup.
        # First call: MISS (cache is empty -> provider called, result stored in cache)
        coalescer.submit("test accessor timing")
        assert provider.call_count == 1, (
            "First submit must be a cache MISS (provider called) — "
            "proves accessor found the newly-installed cache"
        )

        # Second call with same text: HIT (accessor finds cache at submit time,
        # which now has the result from the first MISS written to it)
        coalescer.submit("test accessor timing")
        # On mode HIT: provider NOT called again
        assert provider.call_count == 1, (
            "Second submit must be a cache HIT (accessor-at-submit found the cache); "
            f"but provider was called {provider.call_count} times"
        )


# ===========================================================================
# 3b: Lock-free pre-enqueue cache check
# ===========================================================================


class TestLockFreeCacheCheck:
    """3b: Cache check in submit() must be BEFORE _enqueue and BEFORE governor slot."""

    def test_on_mode_hit_never_calls_enqueue_no_governor_slot(self, monkeypatch):
        """on-mode HIT: _enqueue is NOT called, no governor slot consumed.

        We verify by saturating the governor (all K slots held) then
        submitting — a cache HIT must return immediately without waiting
        for a slot.
        """
        import time
        from code_indexer.server.services import governed_call

        text = "test on mode hit"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # Saturate ALL slots
        hold = threading.Event()
        blockers = _saturate(gov, LANE, hold)

        try:
            # Submit text that is a cache HIT — must return immediately
            # even though all governor slots are held
            t_start = time.monotonic()
            result = coalescer.submit(text)
            elapsed = time.monotonic() - t_start

            # Must return within 0.5s (fast, no slot wait)
            assert elapsed < 0.5, (
                f"on-mode HIT took {elapsed:.2f}s — suspect it waited for a governor slot "
                f"(it should return immediately from cache, no slot needed)"
            )
            # Must return cached vector, not live
            assert result == pytest.approx(CACHED_VEC, abs=1e-4), (
                f"on-mode HIT must return CACHED_VEC {CACHED_VEC}, got {result}"
            )
            # Provider NOT called
            assert provider.call_count == 0, (
                f"on-mode HIT must not call provider, but call_count={provider.call_count}"
            )
        finally:
            hold.set()
            for t in blockers:
                t.join(timeout=5)

    def test_on_mode_hit_zero_provider_embed_calls(self, monkeypatch):
        """on-mode HIT: provider.get_embeddings_batch is NEVER called."""
        from code_indexer.server.services import governed_call

        text = "cache hit test query"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        result = coalescer.submit(text)
        assert provider.call_count == 0
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)

    def test_on_mode_miss_calls_enqueue_provider_embeds(self, monkeypatch):
        """on-mode MISS: _enqueue IS called, provider embedding happens."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="on")  # empty cache = all misses
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        result = coalescer.submit("unique query for miss test")
        assert provider.call_count == 1, "MISS must call provider exactly once"
        assert isinstance(result, list)

    def test_on_mode_miss_then_hit_counter_behavior(self, monkeypatch):
        """on-mode: MISS calls provider; second identical call is HIT (no provider call)."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="on")  # empty cache
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        text = "repeated query text"

        # First: MISS -> provider called
        result1 = coalescer.submit(text)
        assert provider.call_count == 1

        # Second: HIT -> provider NOT called again
        result2 = coalescer.submit(text)
        assert provider.call_count == 1, (
            "Second submit for same text must be a cache HIT (no additional provider call)"
        )
        # Both results should be equivalent (first was live, second is cached)
        assert len(result1) == len(result2) == DIM

    def test_shadow_mode_always_embeds_live(self, monkeypatch):
        """shadow-mode: ALWAYS calls provider even on key HIT."""
        from code_indexer.server.services import governed_call

        text = "shadow mode query"
        cache, _ = _make_real_cache(mode="shadow", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        result = coalescer.submit(text)
        # Shadow mode: always live, even on HIT
        assert provider.call_count == 1, (
            "shadow-mode must ALWAYS call provider (even on key HIT)"
        )
        # Returns LIVE vector, not cached
        expected_live = [float(len(text) % 999), 0.0, 0.0]
        assert result == pytest.approx(expected_live, abs=1e-4)

    def test_shadow_mode_records_hit_once_per_key(self, monkeypatch):
        """shadow-mode HIT: record_hit (touch_last_used) called once per key-resolution."""
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            build_key,
        )

        text = "shadow cosine test"
        backend = _FakeBackend()
        cache = QueryEmbeddingCache(backend, enabled=True, voyage_mode="shadow")
        cache.mode_for = lambda pname: "shadow"  # type: ignore[method-assign]

        # Pre-seed the cache
        key = build_key(text, config_digest=_TEST_DIGEST)
        assert key is not None
        backend._store[(key, PROVIDER_NAME, MODEL, DIM)] = _enc(CACHED_VEC)

        hit_count = [0]
        original_record_hit = cache.record_hit

        def track_hit(k, q):
            hit_count[0] += 1
            original_record_hit(k, q)

        cache.record_hit = track_hit  # type: ignore[method-assign]

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit(text)
        # Shadow HIT: record_hit called once (touch last_used)
        assert hit_count[0] == 1, (
            f"shadow-mode must call record_hit exactly once per key-resolution, "
            f"got {hit_count[0]}"
        )

    def test_off_mode_direct_path_no_cache_ops(self, monkeypatch):
        """off-mode: direct path, no cache lookup or write."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="off")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        lookup_count = [0]
        original_lookup = cache.lookup

        def track_lookup(*args, **kwargs):
            lookup_count[0] += 1
            return original_lookup(*args, **kwargs)

        cache.lookup = track_lookup  # type: ignore[method-assign]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit("off mode query")
        # off-mode: no cache ops
        assert lookup_count[0] == 0, "off-mode must NOT call cache.lookup"
        # Provider was called (live path)
        assert provider.call_count == 1

    def test_cache_disabled_for_provider_direct_path(self, monkeypatch):
        """Cache not enabled for provider: direct path, no cache ops."""
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _FakeBackend()
        cache = QueryEmbeddingCache(backend, enabled=False, voyage_mode="on")
        # enabled_for returns False -> cache skipped
        cache.enabled_for = lambda pname: False  # type: ignore[method-assign]

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit("query for disabled provider")
        # Provider called (live path, cache skipped)
        assert provider.call_count == 1

    def test_cache_io_not_under_lock(self, monkeypatch):
        """Cache I/O (lookup/upsert) must NOT run while self._lock is held.

        We verify by monkey-patching cache.lookup to check if the coalescer's
        internal _lock is currently held when it fires. If it IS held, the
        cache I/O runs under lock — a violation.
        """
        from code_indexer.server.services import governed_call

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        cache, _ = _make_real_cache(mode="on")
        lock_was_held_during_lookup = [False]
        original_lookup = cache.lookup

        def spy_lookup(*args, **kwargs):
            # Try to acquire the coalescer lock with zero timeout.
            # If it can be acquired, the lock is NOT held during lookup (correct).
            # If it CANNOT (timeout), the lock IS held (violation).
            acquired = coalescer._lock.acquire(blocking=False)
            if acquired:
                coalescer._lock.release()
                # lock was NOT held (correct)
            else:
                lock_was_held_during_lookup[0] = True
            return original_lookup(*args, **kwargs)

        cache.lookup = spy_lookup  # type: ignore[method-assign]
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        coalescer.submit("lock free test query")

        assert not lock_was_held_during_lookup[0], (
            "Cache lookup must NOT run while the coalescer's internal _lock is held. "
            "Move cache I/O to BEFORE _enqueue(), outside the lock."
        )


# ===========================================================================
# 3b: Cache metrics integration
# ===========================================================================


class TestCacheMetricsIntegration:
    """3b: Cache metrics (hit/miss counters) are recorded in coalescer's submit()."""

    def test_on_mode_hit_records_hit_metric(self, monkeypatch):
        """on-mode HIT: cache.record_hit must be called."""
        from code_indexer.server.services import governed_call

        text = "metric test hit"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        hit_calls = [0]
        original = cache.record_hit

        def track(*args, **kwargs):
            hit_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_hit = track  # type: ignore[method-assign]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit(text)
        assert hit_calls[0] >= 1, "on-mode HIT must call cache.record_hit"
        assert provider.call_count == 0

    def test_on_mode_miss_records_miss(self, monkeypatch):
        """on-mode MISS: cache.record_miss_or_shadow must be called."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="on")  # empty, all misses
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        upsert_calls = [0]
        original = cache.record_miss_or_shadow

        def track(*args, **kwargs):
            upsert_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_miss_or_shadow = track  # type: ignore[method-assign]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit("miss test query")
        assert upsert_calls[0] >= 1, (
            "on-mode MISS must call cache.record_miss_or_shadow"
        )
        assert provider.call_count == 1


# ===========================================================================
# 3c: Thin-shim coalesced_query_embedding + direct-fallback-still-cache-checks
# ===========================================================================


class TestThinShimCQE:
    """3c: coalesced_query_embedding becomes a thin shim; direct fallback still checks cache."""

    def test_coalesced_query_embedding_routes_through_coalescer_on_cache_miss(
        self, monkeypatch
    ):
        """When a coalescer registry is installed and cache misses, CQE routes
        through coalescer.submit() which handles the live embed + cache write."""
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.coalescer_registry import (
            CoalescerRegistry,
            set_coalescer_registry,
            clear_coalescer_registry,
        )

        cache, _ = _make_real_cache(mode="on")  # empty, all misses
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # Wire the coalescer into the registry
        class _FakeConfig:
            coalesce_enabled = True

        class _FakeConfigService:
            def get_config(self):
                return _FakeConfig()

        monkeypatch.setattr(
            governed_call,
            "get_config_service",
            lambda: _FakeConfigService(),
            raising=False,
        )

        reg = CoalescerRegistry.__new__(CoalescerRegistry)
        reg._coalescers = {LANE: coalescer}
        reg.get_or_create = lambda lane, digest, prov: coalescer
        set_coalescer_registry(reg)

        try:
            result = governed_call.coalesced_query_embedding(
                provider, "thin shim test query"
            )
            # Provider was called (MISS -> live embed)
            assert provider.call_count >= 1
            assert isinstance(result, list)
        finally:
            clear_coalescer_registry()

    def test_coalesced_query_embedding_on_hit_skips_provider(self, monkeypatch):
        """When cache has a HIT, CQE must skip provider entirely (0 calls)."""
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.coalescer_registry import (
            CoalescerRegistry,
            set_coalescer_registry,
            clear_coalescer_registry,
        )

        text = "thin shim hit query"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        class _FakeConfig:
            coalesce_enabled = True

        class _FakeConfigService:
            def get_config(self):
                return _FakeConfig()

        monkeypatch.setattr(
            governed_call,
            "get_config_service",
            lambda: _FakeConfigService(),
            raising=False,
        )

        reg = CoalescerRegistry.__new__(CoalescerRegistry)
        reg._coalescers = {LANE: coalescer}
        reg.get_or_create = lambda lane, digest, prov: coalescer
        set_coalescer_registry(reg)

        try:
            result = governed_call.coalesced_query_embedding(provider, text)
            # Cache HIT: zero provider calls
            assert provider.call_count == 0, (
                f"on-mode HIT via CQE must skip provider, got call_count={provider.call_count}"
            )
            assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        finally:
            clear_coalescer_registry()

    def test_direct_fallback_still_cache_checks_on_miss(self, monkeypatch):
        """When no coalescer (direct fallback), CQE still checks cache on MISS."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="on")  # empty
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        upsert_calls = [0]
        original = cache.record_miss_or_shadow

        def track(*args, **kwargs):
            upsert_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_miss_or_shadow = track  # type: ignore[method-assign]

        # Stub governed_query_embedding to avoid real HTTP
        live_calls = [0]

        def _fake_governed(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            live_calls[0] += 1
            return LIVE_VEC

        monkeypatch.setattr(
            governed_call, "governed_query_embedding", _fake_governed, raising=False
        )

        class _Provider:
            def get_provider_name(self):
                return PROVIDER_NAME

            def get_current_model(self):
                return MODEL

            def get_model_info(self):
                return {"dimensions": DIM}

        # No registry installed -> direct fallback path
        from code_indexer.server.services.coalescer_registry import (
            clear_coalescer_registry,
        )

        clear_coalescer_registry()

        governed_call.coalesced_query_embedding(_Provider(), "direct fallback test")
        # Direct path still writes to cache on MISS
        assert upsert_calls[0] >= 1, (
            "Direct fallback in CQE must still write to cache on MISS"
        )

    def test_direct_fallback_cache_hit_skips_provider(self, monkeypatch):
        """When no coalescer (direct fallback) AND cache is on-mode HIT, skip provider.

        Pre-seeding uses the ACTUAL digest _digest_for_provider returns for a
        minimal provider without a .config attribute ("fallback-no-config"), since
        the direct fallback in coalesced_query_embedding uses _digest_for_provider
        to compute the cache key.
        """
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.coalescer_registry import _FALLBACK_DIGEST
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            build_key,
        )

        text = "direct fallback hit test"

        # Pre-seed using the actual digest the direct-fallback path will compute
        # (_digest_for_provider on a provider with no .config -> _FALLBACK_DIGEST).
        backend = _FakeBackend()
        cache = QueryEmbeddingCache(
            backend, enabled=True, voyage_mode="on", cohere_mode="on"
        )
        cache.mode_for = lambda pname: "on"  # type: ignore[method-assign]
        direct_key = build_key(text, config_digest=_FALLBACK_DIGEST)
        assert direct_key is not None
        backend._store[(direct_key, PROVIDER_NAME, MODEL, DIM)] = _enc(CACHED_VEC)

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        governed_calls = [0]

        def _fake_governed(
            provider, text, *, embedding_purpose=None, acquire_timeout=30.0
        ):
            governed_calls[0] += 1
            return LIVE_VEC

        monkeypatch.setattr(
            governed_call, "governed_query_embedding", _fake_governed, raising=False
        )

        class _Provider:
            def get_provider_name(self):
                return PROVIDER_NAME

            def get_current_model(self):
                return MODEL

            def get_model_info(self):
                return {"dimensions": DIM}

        from code_indexer.server.services.coalescer_registry import (
            clear_coalescer_registry,
        )

        clear_coalescer_registry()

        result = governed_call.coalesced_query_embedding(_Provider(), text)
        # HIT: governed_query_embedding must NOT be called
        assert governed_calls[0] == 0, (
            "Direct fallback on-mode HIT must skip governed_query_embedding"
        )
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)


# ===========================================================================
# 3d: _Entry slots + bypass behavior
# ===========================================================================


class TestEntrySlots:
    """3d: _Entry.__slots__ must include audit_ctx and no_embedding_cache_shortcut."""

    def test_entry_has_audit_ctx_slot(self):
        """_Entry.__slots__ must contain 'audit_ctx'."""
        assert "audit_ctx" in _Entry.__slots__, (
            f"_Entry.__slots__ must include 'audit_ctx', got: {_Entry.__slots__}"
        )

    def test_entry_has_no_embedding_cache_shortcut_slot(self):
        """_Entry.__slots__ must contain 'no_embedding_cache_shortcut'."""
        assert "no_embedding_cache_shortcut" in _Entry.__slots__, (
            f"_Entry.__slots__ must include 'no_embedding_cache_shortcut', "
            f"got: {_Entry.__slots__}"
        )

    def test_entry_default_audit_ctx_is_none(self):
        """_Entry default audit_ctx must be None."""
        e = _Entry("test text")
        assert e.audit_ctx is None

    def test_entry_default_no_embedding_cache_shortcut_is_false(self):
        """_Entry default no_embedding_cache_shortcut must be False."""
        e = _Entry("test text")
        assert e.no_embedding_cache_shortcut is False

    def test_entry_accepts_audit_ctx_kwarg(self):
        """_Entry must accept audit_ctx as keyword argument."""
        ctx: Dict[str, Any] = {}
        e = _Entry("test text", audit_ctx=ctx)
        assert e.audit_ctx is ctx

    def test_entry_accepts_no_embedding_cache_shortcut_kwarg(self):
        """_Entry must accept no_embedding_cache_shortcut as keyword argument."""
        e = _Entry("test text", no_embedding_cache_shortcut=True)
        assert e.no_embedding_cache_shortcut is True


class TestBypassBehavior:
    """3d: bypass (no_embedding_cache_shortcut=True) skips READ but still WRITES."""

    def test_bypass_skips_cache_read_on_hit(self, monkeypatch):
        """bypass=True: cache READ skipped even when entry exists (on-mode HIT)."""
        from code_indexer.server.services import governed_call

        text = "bypass read skip test"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        lookup_calls = [0]
        original_lookup = cache.lookup

        def track_lookup(*args, **kwargs):
            lookup_calls[0] += 1
            return original_lookup(*args, **kwargs)

        cache.lookup = track_lookup  # type: ignore[method-assign]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # bypass=True: skip the cache READ
        coalescer.submit(text, no_embedding_cache_shortcut=True)

        # Provider was called (went live, not from cache)
        assert provider.call_count == 1, (
            "bypass=True must go live (not serve from cache)"
        )
        # cache.lookup should NOT have been called (bypass skips READ)
        assert lookup_calls[0] == 0, (
            f"bypass=True must skip cache.lookup, but lookup was called {lookup_calls[0]} time(s)"
        )

    def test_bypass_still_writes_cache_after_live_embed(self, monkeypatch):
        """bypass=True: cache WRITE (record_miss_or_shadow) still happens after live embed."""
        from code_indexer.server.services import governed_call

        cache, _ = _make_real_cache(mode="on")  # empty
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        upsert_calls = [0]
        original = cache.record_miss_or_shadow

        def track(*args, **kwargs):
            upsert_calls[0] += 1
            return original(*args, **kwargs)

        cache.record_miss_or_shadow = track  # type: ignore[method-assign]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        coalescer.submit("bypass write test", no_embedding_cache_shortcut=True)

        # bypass=True: still writes to cache after live embed
        assert upsert_calls[0] >= 1, (
            "bypass=True must still write to cache after live embed (record_miss_or_shadow)"
        )
        assert provider.call_count == 1

    def test_bypass_goes_live_not_cached_hit(self, monkeypatch):
        """bypass=True requestor goes LIVE even when cache would HIT.

        When a bypass requestor submits for a text that exists in cache, it
        should go live and receive the live vector (not the cached one).
        """
        from code_indexer.server.services import governed_call

        text = "in flight join test"
        # Cache has a HIT for this text (non-bypass would serve from cache)
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # Bypass=True: must not serve from cache, must go live
        result = coalescer.submit(text, no_embedding_cache_shortcut=True)

        # Provider was called (bypass went live)
        assert provider.call_count == 1
        # Result is the live vector, not the cached one
        expected_live = [float(len(text) % 999), 0.0, 0.0]
        assert result == pytest.approx(expected_live, abs=1e-4), (
            f"bypass=True must return live vector, not cached {CACHED_VEC}"
        )


class TestSubmitSignature:
    """3b/3d: submit() must accept no_embedding_cache_shortcut and audit_ctx kwargs."""

    def test_submit_accepts_no_embedding_cache_shortcut(self):
        """submit() must accept no_embedding_cache_shortcut keyword argument."""
        sig = inspect.signature(EmbeddingCoalescer.submit)
        assert "no_embedding_cache_shortcut" in sig.parameters, (
            "EmbeddingCoalescer.submit must accept no_embedding_cache_shortcut kwarg"
        )
        param = sig.parameters["no_embedding_cache_shortcut"]
        assert param.default is False

    def test_submit_accepts_audit_ctx(self):
        """submit() must accept audit_ctx keyword argument."""
        sig = inspect.signature(EmbeddingCoalescer.submit)
        assert "audit_ctx" in sig.parameters, (
            "EmbeddingCoalescer.submit must accept audit_ctx kwarg"
        )
        param = sig.parameters["audit_ctx"]
        assert param.default is None


# ===========================================================================
# 3d: #1110 deep-fidelity audit stays at FSV chokepoint (NOT moved here)
# ===========================================================================


class TestFidelityAuditStaysAtFSV:
    """3d: The #1110 deep-fidelity audit (_run_deep_fidelity_audit) must NOT be
    moved into the coalescer. It stays at the FilesystemVectorStore chokepoint
    (per-repo, per FSV.search call).
    """

    def test_deep_fidelity_audit_not_imported_in_coalescer(self):
        """embedding_coalescer.py must NOT import _run_deep_fidelity_audit."""
        coalescer_src = _COALESCER_PATH.read_text(encoding="utf-8")

        assert "_run_deep_fidelity_audit" not in coalescer_src, (
            "_run_deep_fidelity_audit must NOT appear in embedding_coalescer.py. "
            "It stays at the FSV chokepoint (Story #1110 invariant)."
        )

    def test_embedding_cache_audit_module_not_imported_in_coalescer(self):
        """embedding_coalescer.py must NOT import embedding_cache_audit."""
        coalescer_src = _COALESCER_PATH.read_text(encoding="utf-8")

        assert "embedding_cache_audit" not in coalescer_src, (
            "embedding_coalescer.py must NOT import embedding_cache_audit "
            "(deep-fidelity audit stays at FSV)"
        )


# ===========================================================================
# Shared helpers for Path-A tests
# ===========================================================================


def _make_coalescer_with_cache(monkeypatch, mode: str, pre_seed_text=None):
    """Build a coalescer wired to a real in-memory cache. Returns (coalescer, provider, cache)."""
    from code_indexer.server.services import governed_call

    cache, _ = _make_real_cache(mode=mode, pre_seed_text=pre_seed_text)
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

    gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
    provider = _FakeVoyageProvider()
    coalescer = EmbeddingCoalescer(
        LANE,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        config_digest=_TEST_DIGEST,
    )
    return coalescer, provider, cache


# ===========================================================================
# Path-A audit_ctx population (BLOCKING 1 — per-requestor sampling draw)
# ===========================================================================


class TestPathAAuditCtxPopulation:
    """BLOCKING 1: on Path A (coalescer present) the requestor's audit_ctx dict
    must be populated on sampled cache HITs. Before the fix, _Entry.audit_ctx
    was stored but never read — FSV's #1110 audit was starved of input.
    """

    def test_on_mode_hit_populates_audit_ctx_when_sampled(self, monkeypatch):
        """Path A on-mode HIT: when audit_sample_rate=1.0, audit_ctx gets populated."""
        from code_indexer.server.services import governed_call

        text = "audit ctx on hit test"
        coalescer, provider, _ = _make_coalescer_with_cache(
            monkeypatch, "on", pre_seed_text=text
        )
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 1.0)

        audit_ctx: Dict[str, Any] = {}
        result = coalescer.submit(text, audit_ctx=audit_ctx)

        assert provider.call_count == 0, "on-mode HIT must not call provider"
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        assert audit_ctx.get("sampled") is True
        assert audit_ctx.get("mode") == "on"
        assert audit_ctx.get("provider") == PROVIDER_NAME
        assert "cached_blob" in audit_ctx
        assert "live_vec" not in audit_ctx, (
            "on-mode HIT must NOT have 'live_vec' (Chunk B re-embeds from cached_blob)"
        )

    def test_on_mode_hit_no_audit_ctx_when_not_sampled(self, monkeypatch):
        """Path A on-mode HIT: when audit_sample_rate=0.0, audit_ctx stays empty."""
        from code_indexer.server.services import governed_call

        text = "audit ctx not sampled test"
        coalescer, _, _ = _make_coalescer_with_cache(
            monkeypatch, "on", pre_seed_text=text
        )
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 0.0)

        audit_ctx: Dict[str, Any] = {}
        coalescer.submit(text, audit_ctx=audit_ctx)

        assert audit_ctx == {}, (
            f"on-mode HIT with rate=0.0 must leave audit_ctx empty, got {audit_ctx}"
        )

    def test_on_mode_miss_leaves_audit_ctx_untouched(self, monkeypatch):
        """Path A on-mode MISS: audit_ctx must remain untouched (empty dict)."""
        from code_indexer.server.services import governed_call

        coalescer, _, _ = _make_coalescer_with_cache(monkeypatch, "on")
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 1.0)

        audit_ctx: Dict[str, Any] = {}
        coalescer.submit("miss query no audit", audit_ctx=audit_ctx)

        assert audit_ctx == {}, (
            f"on-mode MISS must leave audit_ctx empty, got {audit_ctx}"
        )

    def test_shadow_hit_populates_audit_ctx_with_live_vec(self, monkeypatch):
        """Path A shadow HIT: audit_ctx gets cached_blob AND live_vec."""
        from code_indexer.server.services import governed_call

        text = "shadow hit audit ctx test"
        coalescer, provider, _ = _make_coalescer_with_cache(
            monkeypatch, "shadow", pre_seed_text=text
        )
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 1.0)

        audit_ctx: Dict[str, Any] = {}
        coalescer.submit(text, audit_ctx=audit_ctx)

        assert provider.call_count == 1, "shadow mode must always call provider"
        assert audit_ctx.get("sampled") is True
        assert audit_ctx.get("mode") == "shadow"
        assert "cached_blob" in audit_ctx
        assert "live_vec" in audit_ctx
        assert isinstance(audit_ctx["live_vec"], list)

    def test_per_requestor_sampling_independent_draws(self, monkeypatch):
        """Per-requestor draw: two sequential on-mode HITs get independent draws.

        rate=0.5, draws=[0.6, 0.4]:
          first requestor: 0.6 >= 0.5 -> NOT sampled -> audit_ctx empty
          second requestor: 0.4 < 0.5 -> sampled -> audit_ctx populated
        """
        import random as _random_mod
        from code_indexer.server.services import governed_call

        text = "per requestor sampling"
        coalescer, provider, _ = _make_coalescer_with_cache(
            monkeypatch, "on", pre_seed_text=text
        )
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 0.5)

        draw_values = iter([0.6, 0.4])
        original_random = _random_mod.random

        def _patched_random():
            try:
                return next(draw_values)
            except StopIteration:
                return original_random()

        monkeypatch.setattr(_random_mod, "random", _patched_random)

        ctx1: Dict[str, Any] = {}
        ctx2: Dict[str, Any] = {}
        coalescer.submit(text, audit_ctx=ctx1)
        coalescer.submit(text, audit_ctx=ctx2)

        assert provider.call_count == 0, "Both must be on-mode HITs (no provider call)"
        assert ctx1 == {}, (
            f"draw=0.6 >= rate=0.5 -> NOT sampled, must be empty, got {ctx1}"
        )
        assert ctx2.get("sampled") is True, (
            f"draw=0.4 < rate=0.5 -> sampled, must have sampled=True, got {ctx2}"
        )

    def test_audit_ctx_none_is_noop(self, monkeypatch):
        """audit_ctx=None (default) must not cause errors on Path A HIT."""
        from code_indexer.server.services import governed_call

        text = "audit ctx none noop test"
        coalescer, provider, _ = _make_coalescer_with_cache(
            monkeypatch, "on", pre_seed_text=text
        )
        monkeypatch.setattr(governed_call, "_audit_sample_rate_for", lambda pname: 1.0)

        result = coalescer.submit(text)  # audit_ctx=None default
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        assert provider.call_count == 0


# ===========================================================================
# Path-A hit/miss metrics (BLOCKING 2 — metrics.record_hit/miss on all outcomes)
# ===========================================================================


class _FakeMetrics:
    """Fake metrics recorder tracking all record_hit/record_miss/record_shadow_cosine calls."""

    def __init__(self) -> None:
        self.hits: List[dict] = []
        self.misses: List[dict] = []
        self.shadow_cosines: int = 0

    def record_hit(self, *, mode: str, provider: str) -> None:
        self.hits.append({"mode": mode, "provider": provider})

    def record_miss(self, *, mode: str, provider: str) -> None:
        self.misses.append({"mode": mode, "provider": provider})

    def record_shadow_cosine(self, *, cached_blob: bytes, live_vec: list) -> None:
        self.shadow_cosines += 1

    def record_long_key(self, *, provider: str) -> None:
        pass


def _make_coalescer_with_metrics(monkeypatch, mode: str, pre_seed_text=None):
    """Build coalescer with real in-memory cache + fake metrics. Returns (coalescer, provider, metrics)."""
    from code_indexer.server.services import governed_call

    cache, _ = _make_real_cache(mode=mode, pre_seed_text=pre_seed_text)
    metrics = _FakeMetrics()
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    monkeypatch.setattr(
        governed_call, "get_query_embedding_cache_metrics", lambda: metrics
    )

    gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
    provider = _FakeVoyageProvider()
    coalescer = EmbeddingCoalescer(
        LANE,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        config_digest=_TEST_DIGEST,
    )
    return coalescer, provider, metrics


class TestPathAMetrics:
    """BLOCKING 2: hit/miss metric counters on Path A for all cache outcomes.

    Pre-3c _serve_with_cache recorded all outcomes. Post-3c Path A only records
    on-mode HIT; on-mode MISS, shadow HIT, and shadow MISS are missing.
    """

    def test_on_mode_hit_records_hit_metric(self, monkeypatch):
        """Path A on-mode HIT: metrics.record_hit(mode='on', provider=...) called."""
        text = "metric on hit"
        coalescer, provider, metrics = _make_coalescer_with_metrics(
            monkeypatch, "on", pre_seed_text=text
        )

        coalescer.submit(text)

        assert len(metrics.hits) == 1, (
            f"on-mode HIT must record exactly 1 hit metric, got {len(metrics.hits)}"
        )
        assert metrics.hits[0]["mode"] == "on"
        assert metrics.hits[0]["provider"] == PROVIDER_NAME
        assert len(metrics.misses) == 0

    def test_on_mode_miss_records_miss_metric(self, monkeypatch):
        """Path A on-mode MISS: metrics.record_miss(mode='on', provider=...) called."""
        coalescer, provider, metrics = _make_coalescer_with_metrics(monkeypatch, "on")

        coalescer.submit("on mode miss query")

        assert len(metrics.misses) == 1, (
            f"on-mode MISS must record exactly 1 miss metric, got {len(metrics.misses)}"
        )
        assert metrics.misses[0]["mode"] == "on"
        assert metrics.misses[0]["provider"] == PROVIDER_NAME
        assert len(metrics.hits) == 0

    def test_shadow_hit_records_hit_metric_and_cosine(self, monkeypatch):
        """Path A shadow HIT: metrics.record_hit + record_shadow_cosine both called."""
        text = "shadow hit metric test"
        coalescer, provider, metrics = _make_coalescer_with_metrics(
            monkeypatch, "shadow", pre_seed_text=text
        )

        coalescer.submit(text)

        assert len(metrics.hits) == 1, (
            f"shadow HIT must record exactly 1 hit metric, got {len(metrics.hits)}"
        )
        assert metrics.hits[0]["mode"] == "shadow"
        assert metrics.hits[0]["provider"] == PROVIDER_NAME
        assert metrics.shadow_cosines == 1, (
            f"shadow HIT must record 1 shadow cosine, got {metrics.shadow_cosines}"
        )
        assert len(metrics.misses) == 0

    def test_shadow_miss_records_miss_metric(self, monkeypatch):
        """Path A shadow MISS: metrics.record_miss(mode='shadow', provider=...) called."""
        coalescer, provider, metrics = _make_coalescer_with_metrics(
            monkeypatch, "shadow"
        )

        coalescer.submit("shadow miss query no seed")

        assert len(metrics.misses) == 1, (
            f"shadow MISS must record exactly 1 miss metric, got {len(metrics.misses)}"
        )
        assert metrics.misses[0]["mode"] == "shadow"
        assert metrics.misses[0]["provider"] == PROVIDER_NAME
        assert len(metrics.hits) == 0

    def test_metrics_none_does_not_raise(self, monkeypatch):
        """When get_query_embedding_cache_metrics() returns None, no errors on HIT."""
        from code_indexer.server.services import governed_call

        text = "metrics none noop"
        cache, _ = _make_real_cache(mode="on", pre_seed_text=text)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        monkeypatch.setattr(
            governed_call, "get_query_embedding_cache_metrics", lambda: None
        )

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        result = coalescer.submit(text)
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)


# ===========================================================================
# K-concurrent same-key cold MISS: exactly 1 miss metric per key-resolution
# ===========================================================================


class TestKConcurrentSameKeyCardinality:
    """CRITICAL: K concurrent same-key cold-MISS requestors coalesce to 1 batch.
    The dispatch loop records exactly 1 miss metric (once per unique key in the
    dispatched batch), never K misses.
    """

    def test_k_concurrent_same_key_cold_records_exactly_one_miss(self, monkeypatch):
        """K concurrent same-key cold submits -> exactly 1 miss metric (not K)."""
        from code_indexer.server.services import governed_call

        K = 5
        text = "same key cold test"
        cache, _ = _make_real_cache(mode="on")  # empty, all misses
        metrics = _FakeMetrics()

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        monkeypatch.setattr(
            governed_call, "get_query_embedding_cache_metrics", lambda: metrics
        )

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        outcome = _run_saturated_submits(
            coalescer, gov, LANE, [text] * K, accumulate=0.2
        )

        assert not outcome.errors, f"Unexpected errors: {outcome.errors}"
        assert len(outcome.results) == K

        # Dedup: exactly 1 provider call
        assert provider.call_count == 1, (
            f"K same-key cold submits must produce exactly 1 provider call, got {provider.call_count}"
        )

        # Miss metric: exactly 1 (once per unique key in the dispatched batch)
        assert len(metrics.misses) == 1, (
            f"K same-key cold submits must record exactly 1 miss metric, got {len(metrics.misses)}"
        )
        assert metrics.misses[0]["mode"] == "on"
        assert metrics.misses[0]["provider"] == PROVIDER_NAME

    def test_corrupt_blob_logs_warning_not_silent(self, monkeypatch):
        """NIT (reviewer): corrupt blob in on-mode HIT must log a WARNING, not silently pass."""
        import logging
        from code_indexer.server.services import governed_call
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
            build_key,
        )

        text = "corrupt blob warn test"
        backend = _FakeBackend()
        cache = QueryEmbeddingCache(backend, enabled=True, voyage_mode="on")
        cache.mode_for = lambda pname: "on"  # type: ignore[method-assign]

        key = build_key(text, config_digest=_TEST_DIGEST)
        assert key is not None
        # Corrupt: wrong byte count (should be DIM*4=12 bytes, we give 3)
        backend._store[(key, PROVIDER_NAME, MODEL, DIM)] = b"\x01\x02\x03"

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        log_records: List[str] = []

        class _Handler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    log_records.append(record.getMessage())

        handler = _Handler()
        coalescer_logger = logging.getLogger(
            "code_indexer.server.services.embedding_coalescer"
        )
        coalescer_logger.addHandler(handler)
        coalescer_logger.setLevel(logging.DEBUG)

        try:
            # Must not raise — corrupt blob treated as MISS, falls through to live
            coalescer.submit(text)
            assert provider.call_count == 1, "Corrupt blob must fall through to MISS"
        finally:
            coalescer_logger.removeHandler(handler)

        assert any(
            "corrupt" in msg.lower() or "struct" in msg.lower() or "miss" in msg.lower()
            for msg in log_records
        ), f"Corrupt blob must log a WARNING, got: {log_records}"
