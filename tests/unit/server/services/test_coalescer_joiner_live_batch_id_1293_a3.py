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
"""

from __future__ import annotations

import threading
from typing import Any, cast

from tests.unit.server.services.test_coalescer_cache_1147 import (
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
        """After the key is warm, a burst of identical queries records
        role=warm_hit / live_batch_id=None and adds zero provider calls."""
        text = "A3 warm burst"
        coalescer, provider, gov = _make_harness(monkeypatch, "on", pre_seed_text=text)

        results: list = []
        lock = threading.Lock()

        def _one() -> None:
            vec, meta = coalescer.submit(text)
            with lock:
                results.append(meta)

        threads = [threading.Thread(target=_one, daemon=True) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_JOIN_TIMEOUT)

        assert len(results) == 3
        expected_key = _expected_key(text)
        for meta in results:
            assert meta.role == "warm_hit"
            assert meta.outcome == "hit"
            assert meta.live_batch_id is None
            assert meta.embed_key == expected_key, (
                "Story #1295: warm_hit EmbeddingCacheMetadata.embed_key must be "
                f"the real resolved cache key; got {meta.embed_key!r}"
            )
        assert provider.call_count == 0


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
