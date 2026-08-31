"""Story #1293 S1b [A3]: coalescer joiner ``live_batch_id`` wiring.

The batch OWNER (single-flight ``_inflight_keys`` registrant) assigns a
``live_batch_id`` (one sealed batch == one provider HTTP call) BEFORE
completing the shared Future; each JOINER reads ``meta`` after the Future
resolves. A warm/coalesced-into-existing hit has ``live_batch_id=None``.
After this wiring, ``emit_embed_event`` stops no-op'ing for the coalescer
path (every returned ``EmbeddingCacheMetadata`` now has ``role``/``outcome``
populated).

Reuses the real-cache single-flight harness already established by
tests/unit/server/services/test_coalescer_cache_1147.py (Messi #4
anti-duplication) -- real EmbeddingCoalescer, real in-memory cache backend,
real ProviderConcurrencyGovernor, deterministic saturation-based coalescing
(no timing races).

Story #1295 (Epic #1288 final) addition: ``embed_key`` assertions. Discovered
via this story's mandatory front-door E2E exercise -- a live server run
showed EVERY coalescer-path ``search_embed_event`` row had a NULL
``embed_key``, which silently defeats the Story #1295 audit re-source
(``_record_audit_metrics`` requires a non-None ``embed_key`` to key the
``update_audit_by_key`` UPDATE) and is the root cause of issue #1306
("audit columns never populated"). ``_make_hit_meta``/``_make_miss_meta``/
``_make_joiner_meta`` never threaded the resolved cache key onto
``EmbeddingCacheMetadata.embed_key`` -- fixed here.

Bug #1531 Codex review follow-up: the warm-burst test below used to discard
the actual vector returned to every concurrent caller (``vec, meta = ...``
then only ``meta`` was kept) and had no hit-metric assertion at all -- so a
broken Future result (e.g. a joiner receiving ``None`` or a corrupted vector
instead of the correct cached embedding) would still have passed. Fixed by
capturing every caller's ``vec`` and asserting approximate-float equality
against the known cached vector (``pytest.approx``, the same idiom
test_coalescer_cache_1147.py already uses for cache-retrieved vectors, since
the value round-trips through the cache backend's byte encode/decode), plus
wrapping ``cache.record_hit`` with a counting probe (mirroring
test_hit_miss_once_per_key_1148.py's ``_CacheCallProbe``) to assert the
single-flight group records exactly 1 hit metric.
"""

from __future__ import annotations

import threading
from typing import Any, cast

import pytest

from tests.unit.server.services.test_coalescer_cache_1147 import (
    CACHED_VEC,
    GOV_K,
    LANE,
    _FakeVoyageProvider,
    _make_real_cache,
    _run_saturated_submits,
    _TEST_DIGEST,
)

from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

_JOIN_TIMEOUT: float = 10.0
_ACCUMULATE_SECS: float = 0.2
_K_CONCURRENT: int = 5


def _make_harness(monkeypatch, mode: str, pre_seed_text=None):
    from code_indexer.server.services import governed_call

    cache, _ = _make_real_cache(mode=mode, pre_seed_text=pre_seed_text)
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
    provider = _FakeVoyageProvider()
    coalescer = EmbeddingCoalescer(
        LANE, provider, governor=gov, acquire_timeout=5.0, config_digest=_TEST_DIGEST
    )
    return coalescer, provider, gov


def _expected_key(text: str) -> str:
    from code_indexer.server.services.query_embedding_cache import build_key

    key = build_key(text, config_digest=_TEST_DIGEST)
    assert key is not None, f"test setup error: '{text}' unexpectedly over-cap"
    return cast(str, key)


class TestConcurrentColdOwnerJoiner:
    """AC-A3: K concurrent identical cold queries -> 1 owner + (K-1) joiners
    sharing ONE non-null live_batch_id."""

    def test_owner_miss_and_joiners_share_one_live_batch_id(self, monkeypatch):
        coalescer, provider, gov = _make_harness(monkeypatch, "on")
        text = "A3 concurrent cold same key"

        outcome = _run_saturated_submits(
            coalescer, gov, LANE, [text] * _K_CONCURRENT, accumulate=_ACCUMULATE_SECS
        )
        assert not outcome.errors
        assert len(outcome.results) == _K_CONCURRENT

        metas = [cast(Any, meta) for (_vec, meta) in outcome.results.values()]
        owners = [m for m in metas if m.role == "owner"]
        joiners = [m for m in metas if m.role == "joiner"]

        assert len(owners) == 1, f"expected exactly 1 owner, got {len(owners)}"
        assert len(joiners) == _K_CONCURRENT - 1, (
            f"expected {_K_CONCURRENT - 1} joiners, got {len(joiners)}"
        )
        assert owners[0].outcome == "miss"
        assert owners[0].live_batch_id is not None

        owner_batch_id = owners[0].live_batch_id
        expected_key = _expected_key(text)
        assert owners[0].embed_key == expected_key, (
            "Story #1295: owner's EmbeddingCacheMetadata.embed_key must be the "
            f"real resolved cache key; got {owners[0].embed_key!r}"
        )
        for j in joiners:
            assert j.outcome == "hit"
            assert j.live_batch_id == owner_batch_id, (
                "every joiner must share the owner's live_batch_id"
            )
            assert j.live_batch_id is not None, "a joiner must never record NULL"
            assert j.embed_key == expected_key, (
                "Story #1295: joiner's EmbeddingCacheMetadata.embed_key must "
                f"match the owner's resolved key; got {j.embed_key!r}"
            )

        # Real invariant: exactly ONE HTTP call for the whole cold group.
        assert provider.call_count == 1

    def test_warm_burst_after_cold_uses_warm_hit_with_null_batch_id(self, monkeypatch):
        """After the key is warm, a burst of identical queries all resolve
        with outcome=hit / live_batch_id=None and adds zero provider calls.

        Bug #1531 correction: the original version started 3 unsynchronized
        threads and asserted ``role == "warm_hit"`` for ALL of them. That only
        held by scheduling luck -- the single-flight registry (Story #1148
        PART 1) covers on-mode HITs too, so a same-key requestor that reaches
        the inflight check while the owner's ``cache.lookup`` critical section
        is still running becomes a genuine JOINER (role="joiner"), not a
        second independent warm_hit. Under CPU contention (a full
        server-fast-automation.sh run) the GIL can switch mid-critical-section
        often enough to flip a thread from warm_hit to joiner, which is the
        exact load-sensitive flake this bug reports.

        This test now FORCES full overlap deterministically (delaying the
        owner's ``cache.lookup`` via an Event, the same accumulate-window
        technique ``_run_saturated_submits`` already uses for the MISS path)
        and asserts the real single-flight invariant: exactly 1 owner
        (role="warm_hit") + 2 joiners (role="joiner"), all outcome=hit,
        live_batch_id=None (a joiner shares the owner's live_batch_id
        verbatim, which is None for a warm-HIT resolution), correct embed_key,
        zero provider calls.

        Codex review follow-up (Bug #1531): also asserts (a) every one of the
        n_threads concurrent callers received the EXACT expected cached
        vector (not just "no exception" on role/outcome metadata -- a broken
        Future result returning None or a corrupted vector would still have
        passed the pre-fix version of this test), and (b) the cache records
        exactly 1 hit metric for the whole coalesced group (mirroring
        test_hit_miss_once_per_key_1148.py's hit-cardinality assertion,
        which this file previously lacked entirely).
        """
        import time

        from code_indexer.server.services import governed_call

        text = "A3 warm burst"
        n_threads = 3
        coalescer, provider, gov = _make_harness(monkeypatch, "on", pre_seed_text=text)
        cache = governed_call.get_query_embedding_cache()

        # Hit-metric probe (mirrors test_hit_miss_once_per_key_1148.py's
        # _CacheCallProbe): counts real cache.record_hit calls so we can
        # assert the coalesced group records exactly 1 hit, not n_threads.
        hit_count = {"n": 0}
        hit_lock = threading.Lock()
        orig_record_hit = cache.record_hit

        def _counting_record_hit(key, qualifier):
            with hit_lock:
                hit_count["n"] += 1
            return orig_record_hit(key, qualifier)

        monkeypatch.setattr(cache, "record_hit", _counting_record_hit)

        lookup_started = threading.Event()
        release = threading.Event()
        orig_lookup = cache.lookup

        def _delayed_lookup(key, qualifier):
            lookup_started.set()
            release.wait(timeout=_JOIN_TIMEOUT)
            return orig_lookup(key, qualifier)

        monkeypatch.setattr(cache, "lookup", _delayed_lookup)

        barrier = threading.Barrier(n_threads)
        results: list = []
        lock = threading.Lock()

        def _one() -> None:
            barrier.wait()
            vec, meta = coalescer.submit(text)
            with lock:
                results.append((vec, meta))

        threads = [threading.Thread(target=_one, daemon=True) for _ in range(n_threads)]
        for t in threads:
            t.start()

        assert lookup_started.wait(timeout=_JOIN_TIMEOUT), (
            "owner never reached the delayed cache.lookup call"
        )
        time.sleep(_ACCUMULATE_SECS)
        release.set()

        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)

        assert len(results) == n_threads
        expected_key = _expected_key(text)

        metas = [m for (_vec, m) in results]
        owners = [m for m in metas if m.role == "warm_hit"]
        joiners = [m for m in metas if m.role == "joiner"]
        assert len(owners) == 1, (
            f"expected exactly 1 warm_hit owner, got {len(owners)} "
            f"(roles={[m.role for m in metas]})"
        )
        assert len(joiners) == n_threads - 1, (
            f"expected {n_threads - 1} joiners, got {len(joiners)} "
            f"(roles={[m.role for m in metas]})"
        )
        for meta in metas:
            assert meta.outcome == "hit"
            assert meta.live_batch_id is None
            assert meta.embed_key == expected_key, (
                "Story #1295: embed_key must be the real resolved cache key; "
                f"got {meta.embed_key!r}"
            )
        assert provider.call_count == 0

        # Codex review finding (Bug #1531): verify the ACTUAL vector value
        # every caller (owner and every joiner) received, not just the
        # metadata shape. A broken Future result would still pass every
        # assertion above. pytest.approx is used (not `==`) because the
        # vector round-trips through the cache backend's byte encode/decode
        # -- the same comparison idiom test_coalescer_cache_1147.py already
        # uses for cache-retrieved vectors.
        for i, (vec, meta) in enumerate(results):
            assert vec == pytest.approx(CACHED_VEC, abs=1e-4), (
                f"caller {i} (role={meta.role!r}) must receive the exact "
                f"cached vector {CACHED_VEC}, got {vec!r}"
            )

        # Codex review finding (Bug #1531): this file previously had no
        # hit-metric assertion at all -- mirror test_hit_miss_once_per_key_
        # 1148.py's cardinality check: single-flight must record exactly 1
        # hit for the whole coalesced warm-burst group, not n_threads.
        assert hit_count["n"] == 1, (
            f"single-flight warm burst must record exactly 1 hit metric for "
            f"the coalesced group, got hits={hit_count['n']}"
        )


class TestShadowModeEmbedKey:
    """Story #1295: shadow-mode dispatch-loop HIT/MISS metadata must also
    carry the real resolved embed_key (the dispatch loop's own
    _make_hit_meta/_make_miss_meta call sites, distinct from the on-mode
    owner/joiner call sites above)."""

    def test_shadow_hit_has_embed_key(self, monkeypatch):
        text = "A3 shadow hit embed_key"
        coalescer, _provider, _gov = _make_harness(
            monkeypatch, "shadow", pre_seed_text=text
        )

        vec, meta = coalescer.submit(text)

        assert meta.outcome == "hit"
        assert meta.embed_key == _expected_key(text), (
            f"Story #1295: shadow HIT embed_key must be the real resolved "
            f"cache key; got {meta.embed_key!r}"
        )

    def test_shadow_miss_has_embed_key(self, monkeypatch):
        text = "A3 shadow miss embed_key"
        coalescer, _provider, _gov = _make_harness(monkeypatch, "shadow")

        vec, meta = coalescer.submit(text)

        assert meta.outcome == "shadow_miss"
        assert meta.embed_key == _expected_key(text), (
            f"Story #1295: shadow MISS embed_key must be the real resolved "
            f"cache key; got {meta.embed_key!r}"
        )
