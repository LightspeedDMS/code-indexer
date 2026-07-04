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
    monkeypatch.setattr(
        governed_call, "get_query_embedding_cache_metrics", lambda: None
    )
    gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
    provider = _FakeVoyageProvider()
    coalescer = EmbeddingCoalescer(
        LANE, provider, governor=gov, acquire_timeout=5.0, config_digest=_TEST_DIGEST
    )
    return coalescer, provider, gov


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
        for j in joiners:
            assert j.outcome == "hit"
            assert j.live_batch_id == owner_batch_id, (
                "every joiner must share the owner's live_batch_id"
            )
            assert j.live_batch_id is not None, "a joiner must never record NULL"

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
        for meta in results:
            assert meta.role == "warm_hit"
            assert meta.outcome == "hit"
            assert meta.live_batch_id is None
        assert provider.call_count == 0
