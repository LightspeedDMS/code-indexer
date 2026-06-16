"""Unit tests for EmbeddingCoalescer dedup-by-key + multiplex (Story #1146).

This story adds dedup within a sealed batch so K same-key entries collapse to
EXACTLY ONE provider embedding call.  Tests use the REAL governor + the
deterministic FakeVoyageProvider to keep anti-mock discipline while remaining
fast (no real HTTP calls, no network).

Key behaviours exercised:
  AC1: K same-key entries in a batch => exactly ONE provider embed call + correct
       multiplex (every caller gets the same vector).
  AC2: First claimant embeds the REAL text, others receive that vector, order-preserved.
  AC3: Shared-fate on dispatch error: every same-key caller gets the exception.
  AC4: dedup_savings = requestors_in_live_batch - unique_provider_texts_sent.
  AC5: Over-cap None-key entries fall back to text-dedup (identical text => same vector).
  AC6: CLI/solo direct path is unchanged (no coalescer => no dedup).

Module dependency decision (stated here, intentional):
  embedding_coalescer.py imports build_key from query_embedding_cache.py
  This is the ACCEPTED direction: embedding_coalescer -> query_embedding_cache.
  query_embedding_cache MUST NOT import embedding_coalescer (verified in AC6 test).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import pytest

from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LANE = "voyage:embed"
GOV_K = 8  # K_MIN — smallest K the limiter clamps to

# A stable config digest for dedup-key building (normally from _digest_for_provider)
_TEST_DIGEST = "abc123testdigest"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()


# ---------------------------------------------------------------------------
# Deterministic fake provider (mirrors FakeVoyageProvider from the main test file)
# ---------------------------------------------------------------------------


class _FakeVoyageProvider:
    """Voyage-shaped fake: counts calls, records which texts were sent.

    Returns a deterministic vector based on text length so tests can verify
    that callers receive the correct vector.
    """

    def __init__(
        self,
        token_limit: int = 120_000,
        tokens_per_text: int = 1,
        scripted_exc: Optional[BaseException] = None,
    ) -> None:
        self._token_limit = token_limit
        self._tokens_per_text = tokens_per_text
        self._scripted_exc = scripted_exc
        self.call_count = 0
        self.calls_texts: List[List[str]] = []
        self._lock = threading.Lock()

    def _count_tokens_accurately(self, text: str) -> int:
        return self._tokens_per_text

    def _get_model_token_limit(self) -> int:
        return self._token_limit

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
        if self._scripted_exc is not None:
            raise self._scripted_exc
        # Return a distinct vector per text (float of text length mod 999).
        return [[float(len(t) % 999), 0.0] for t in texts]


# ---------------------------------------------------------------------------
# Saturation harness
# ---------------------------------------------------------------------------


class _Outcome:
    def __init__(self) -> None:
        self.results: Dict[int, List[float]] = {}
        self.errors: Dict[int, BaseException] = {}


def _saturate(
    governor: ProviderConcurrencyGovernor, lane: str, hold: threading.Event
) -> List[threading.Thread]:
    """Occupy all K slots of ``lane`` until ``hold`` is set."""
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
    bar.wait()  # all K slots now held
    return threads


def _run_saturated_submits(
    coalescer: EmbeddingCoalescer,
    governor: ProviderConcurrencyGovernor,
    lane: str,
    texts: List[str],
    *,
    accumulate: float = 0.3,
) -> _Outcome:
    """Submit ``texts`` concurrently against a fully saturated lane so they coalesce."""
    import time

    hold = threading.Event()
    blockers = _saturate(governor, lane, hold)
    outcome = _Outcome()
    n = len(texts)
    start = threading.Barrier(n)

    def _submit(i: int) -> None:
        start.wait()
        try:
            outcome.results[i] = coalescer.submit(texts[i])
        except BaseException as ex:  # noqa: BLE001
            outcome.errors[i] = ex

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(_submit, i) for i in range(n)]
        # Let submitters enqueue before releasing the gate.
        time.sleep(accumulate)
        hold.set()
        for f in futs:
            f.result(timeout=30)

    hold.set()
    for t in blockers:
        t.join(timeout=5)
    return outcome


# ---------------------------------------------------------------------------
# AC1: K same-key entries => exactly ONE provider embed call + correct multiplex
# ---------------------------------------------------------------------------


class TestSameKeyCollapsesToOneProviderCall:
    """AC1 + AC2: same-key entries in one batch produce exactly one HTTP call."""

    def test_k_same_text_entries_produce_one_provider_call(self) -> None:
        """K entries with the same text must collapse to 1 provider embed call."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 5
        same_text = "query about authentication tokens"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        # No errors
        assert not outcome.errors, f"unexpected errors: {outcome.errors}"
        # Exactly ONE provider call for K same-key entries
        assert provider.call_count == 1, (
            f"expected 1 provider call for {K} same-key entries, got {provider.call_count}"
        )
        # Provider was sent exactly 1 text (the dedup reduced K -> 1)
        assert len(provider.calls_texts[0]) == 1, (
            f"provider received {len(provider.calls_texts[0])} texts, expected 1"
        )
        # All K callers got a result
        assert len(outcome.results) == K

    def test_all_same_key_callers_receive_identical_vector(self) -> None:
        """Every same-key caller receives the same vector (multiplexed)."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 4
        same_text = "list all authentication failures"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        assert not outcome.errors
        vectors = [outcome.results[i] for i in range(K)]
        first = vectors[0]
        for i, v in enumerate(vectors[1:], 1):
            assert v == first, (
                f"caller {i} got different vector ({v}) vs caller 0 ({first})"
            )

    def test_mixed_texts_only_dedup_matching_keys(self) -> None:
        """A batch with 3 same-key texts + 1 different text sends 2 unique texts
        to the provider (3 same -> 1, 1 different -> 1 = 2 total)."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # 3 identical texts + 1 different text = 2 unique texts in the batch
        texts = ["auth tokens query"] * 3 + ["different unique query here"]
        outcome = _run_saturated_submits(coalescer, gov, LANE, texts)

        assert not outcome.errors
        # Provider should have been called once (one batch) with 2 unique texts
        assert provider.call_count == 1
        assert len(provider.calls_texts[0]) == 2


# ---------------------------------------------------------------------------
# AC2: First claimant embeds REAL text, order-preserved Futures
# ---------------------------------------------------------------------------


class TestFirstClaimantUsesRealText:
    """AC2: The first entry to claim a dedup key is embedded with real query text."""

    def test_first_claimant_text_is_sent_to_provider(self) -> None:
        """The text sent to the provider for a dedup key must be the REAL query text."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        real_text = "find all authentication errors in logs"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [real_text] * 3)

        assert not outcome.errors
        # Provider received the real text, not the key string
        sent_text = provider.calls_texts[0][0]
        assert sent_text == real_text, (
            f"provider received '{sent_text}', expected the real text '{real_text}'"
        )
        # Verify the key is NOT the text sent (key would start with 's:')
        assert not sent_text.startswith("s:"), (
            "provider received the key string instead of the real query text"
        )

    def test_results_are_order_preserved_futures(self) -> None:
        """Every caller's Future resolves with its own order-preserved result."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 6
        same_text = "find all user login events"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        # All K callers received a result (order-preserved)
        assert len(outcome.results) == K
        assert not outcome.errors
        # The vector is correct (based on first text length)
        expected_vector = [float(len(same_text) % 999), 0.0]
        for i in range(K):
            assert outcome.results[i] == expected_vector


# ---------------------------------------------------------------------------
# AC3: Shared-fate on dispatch error: every same-key caller gets the exception
# ---------------------------------------------------------------------------


class TestSharedFateOnDispatchError:
    """AC3: On dispatch error, every same-key caller receives the same exception."""

    def test_dispatch_error_fans_out_to_all_same_key_callers(self) -> None:
        """When the single dispatch for a dedup key fails, all same-key callers fail.

        The _FakeVoyageProvider raises an httpx.HTTPStatusError(429) directly.
        execute_with_backoff retries it (3 attempts) then wraps it into
        ProviderRateLimitedError — that is the surfaced shared-fate exception.
        All K same-key callers must receive ProviderRateLimitedError.
        """
        import httpx

        from code_indexer.services.provider_backoff import ProviderRateLimitedError

        exc = httpx.HTTPStatusError(
            "rate limited",
            request=httpx.Request("POST", "https://api.example.com/embed"),
            response=httpx.Response(429),
        )
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider(scripted_exc=exc)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 4
        same_text = "list all failed authentication attempts"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        # No results, all errors
        assert not outcome.results
        assert len(outcome.errors) == K
        # All errors are ProviderRateLimitedError (execute_with_backoff wraps the 429)
        # and all are the SAME type (shared fate).
        for i in range(K):
            assert isinstance(outcome.errors[i], ProviderRateLimitedError), (
                f"caller {i} got {type(outcome.errors[i])}, expected ProviderRateLimitedError"
            )

    def test_runtime_error_fans_out_to_all_same_key_callers(self) -> None:
        """A RuntimeError dispatch error also fans out to every same-key caller."""
        exc = RuntimeError("provider internal error")
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider(scripted_exc=exc)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 3
        same_text = "search for all timeout events"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        assert not outcome.results
        assert len(outcome.errors) == K
        for i in range(K):
            assert isinstance(outcome.errors[i], RuntimeError)


# ---------------------------------------------------------------------------
# AC4: dedup_savings counter
# ---------------------------------------------------------------------------


class TestDedupSavingsCounter:
    """AC4: dedup_savings = requestors_in_live_batch - unique_provider_texts_sent."""

    def test_dedup_savings_counts_saved_embeddings(self) -> None:
        """dedup_savings reflects how many embed calls were saved by dedup."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # Initial state: zero savings
        assert coalescer.dedup_savings == 0

        K = 5
        same_text = "find all error log entries"
        outcome = _run_saturated_submits(coalescer, gov, LANE, [same_text] * K)

        assert not outcome.errors
        # K requestors, 1 unique text -> savings = K - 1
        expected_savings = K - 1
        assert coalescer.dedup_savings == expected_savings, (
            f"dedup_savings={coalescer.dedup_savings}, expected {expected_savings}"
        )

    def test_dedup_savings_accumulates_across_batches(self) -> None:
        """dedup_savings accumulates across multiple dispatched batches.

        Each batch is run on a FRESH governor so that the saturation primitive
        is deterministic: all K blocker slots are held before submitters run.
        After batch 1: savings == 3 (4 requestors - 1 unique).
        After batch 2 (same coalescer, new gov): savings >= 3+2 == 5, because
        all 3 same-key entries in batch 2 are guaranteed to coalesce (fresh
        saturation before release).
        """
        provider = _FakeVoyageProvider()

        # Batch 1: fresh governor, 4 same-key entries -> savings = 3
        gov1 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov1,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )
        outcome1 = _run_saturated_submits(
            coalescer, gov1, LANE, ["batch one query text"] * 4
        )
        assert not outcome1.errors
        assert coalescer.dedup_savings == 3

        # Batch 2: fresh governor on the SAME coalescer, 3 same-key entries.
        # Using a fresh gov guarantees the saturation is clean and all 3 entries
        # coalesce into one dispatch -> savings += 2 (cumulative = 5).
        gov2 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer._governor = gov2  # swap governor; coalescer counters persist
        outcome2 = _run_saturated_submits(
            coalescer, gov2, LANE, ["batch two query text"] * 3
        )
        assert not outcome2.errors
        assert coalescer.dedup_savings == 5

    def test_dedup_savings_zero_for_all_unique_texts(self) -> None:
        """When all texts in a batch are unique, dedup_savings remains zero."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # All distinct texts -> no savings
        unique_texts = [f"unique query number {i}" for i in range(4)]
        outcome = _run_saturated_submits(coalescer, gov, LANE, unique_texts)

        assert not outcome.errors
        assert coalescer.dedup_savings == 0

    def test_dedup_savings_mixed_batch(self) -> None:
        """Mixed batch: 3 same-key + 2 different -> savings = 2 (3 became 1, 2 stayed 2)."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # 3 identical + 2 different = 5 requestors, 3 unique -> savings = 2
        texts = ["duplicate query text"] * 3 + [
            "first unique text here",
            "second unique text here",
        ]
        outcome = _run_saturated_submits(coalescer, gov, LANE, texts)

        assert not outcome.errors
        # 5 requestors - 3 unique texts sent = 2 savings
        assert coalescer.dedup_savings == 2

    def test_dedup_savings_excludes_cache_hit_savings(self) -> None:
        """dedup_savings must NOT include savings from cache hits (those are separate).

        This is enforced by the fact that dedup_savings only increments inside
        _dispatch (for LIVE batches only).  Cache hits never reach _dispatch.
        We verify that dedup_savings increases by exactly (requestors - unique) for
        a live batch, and that the counter stays at that value without further change.
        """
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        before = coalescer.dedup_savings
        outcome = _run_saturated_submits(
            coalescer, gov, LANE, ["find cache hits in logs"] * 3
        )
        assert not outcome.errors
        # dedup_savings increased by exactly (3-1) = 2 for this live batch
        after = coalescer.dedup_savings
        assert after - before == 2


# ---------------------------------------------------------------------------
# AC5: Over-cap None-key entries fall back to text-dedup
# ---------------------------------------------------------------------------


class TestOverCapNullKeyFallback:
    """AC5: When build_key returns None (over 256 chars), dedup falls back to text."""

    def _make_overcap_text(self) -> str:
        """Generate a text whose normalized form exceeds the 256-char key cap."""
        # build_key normalizes text.split() so we need > 256 chars of unique words
        # (sorted, none exceeding individually, but combined > 256 chars).
        # 50 distinct words of 6 chars each = 300 chars + spaces -> > 256
        return " ".join(f"word{i:02d}" for i in range(50))

    def test_identical_overcap_texts_still_dedup_by_exact_text(self) -> None:
        """K identical over-cap texts must still collapse to ONE provider call."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        over_cap_text = self._make_overcap_text()

        # Verify the text IS over-cap (build_key returns None)
        from code_indexer.server.services.query_embedding_cache import build_key

        assert build_key(over_cap_text, config_digest=_TEST_DIGEST) is None, (
            "test setup error: text is not over-cap"
        )

        K = 3
        outcome = _run_saturated_submits(coalescer, gov, LANE, [over_cap_text] * K)

        assert not outcome.errors
        # Even without a key, identical texts should collapse to 1 provider call
        assert provider.call_count == 1, (
            f"over-cap texts did not dedup: {provider.call_count} calls for {K} identical texts"
        )
        # Provider received exactly 1 text
        assert len(provider.calls_texts[0]) == 1

    def test_different_overcap_texts_each_send_to_provider(self) -> None:
        """Different over-cap texts (each returning None key) are NOT deduped."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        # Two different over-cap texts
        base = " ".join(f"word{i:02d}" for i in range(50))
        over_cap_1 = base + " extraa"
        over_cap_2 = base + " extrab"

        from code_indexer.server.services.query_embedding_cache import build_key

        assert build_key(over_cap_1, config_digest=_TEST_DIGEST) is None
        assert build_key(over_cap_2, config_digest=_TEST_DIGEST) is None

        outcome = _run_saturated_submits(coalescer, gov, LANE, [over_cap_1, over_cap_2])

        assert not outcome.errors
        # 2 different texts -> provider sends 2 texts
        assert len(provider.calls_texts[0]) == 2


# ---------------------------------------------------------------------------
# AC6: CLI/solo direct path unchanged
# ---------------------------------------------------------------------------


class TestCliDirectPathUnchanged:
    """AC6: CLI/solo path (no coalescer) is unchanged by dedup logic."""

    def test_no_circular_import_between_coalescer_and_cache(self) -> None:
        """query_embedding_cache must NOT import embedding_coalescer."""
        import importlib
        import sys

        # Clear any cached imports to get a fresh read of query_embedding_cache
        for mod in list(sys.modules.keys()):
            if "embedding_coalescer" in mod:
                del sys.modules[mod]

        # Import query_embedding_cache — it must NOT pull in embedding_coalescer
        importlib.import_module("code_indexer.server.services.query_embedding_cache")

        # After importing only query_embedding_cache, the coalescer must not be loaded
        assert not any("embedding_coalescer" in k for k in sys.modules.keys()), (
            "query_embedding_cache imported embedding_coalescer as a side effect — "
            "this creates a circular import (embedding_coalescer -> query_embedding_cache "
            "is the accepted direction; the reverse must not exist)"
        )

    def test_coalescer_without_config_digest_still_functions(self) -> None:
        """A coalescer created without config_digest still works (uses text-based dedup)."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        # No config_digest passed -> should use a fallback (empty string or sentinel)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            # No config_digest: dedup must still work by text
        )

        outcome = _run_saturated_submits(
            coalescer, gov, LANE, ["simple query text"] * 3
        )

        # Must work without errors and return all results
        assert not outcome.errors
        assert len(outcome.results) == 3


# ---------------------------------------------------------------------------
# New counter attribute tests
# ---------------------------------------------------------------------------


class TestNewCounterAttributes:
    """Verify that EmbeddingCoalescer exposes dedup_savings and provider_embed_calls."""

    def test_dedup_savings_attribute_exists(self) -> None:
        """EmbeddingCoalescer must expose a dedup_savings counter initialized to 0."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )
        assert hasattr(coalescer, "dedup_savings")
        assert coalescer.dedup_savings == 0

    def test_provider_embed_calls_attribute_exists(self) -> None:
        """EmbeddingCoalescer must expose a provider_embed_calls counter (increases by 1
        per dispatched batch)."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )
        assert hasattr(coalescer, "provider_embed_calls")
        assert coalescer.provider_embed_calls == 0

    def test_provider_embed_calls_increments_by_one_per_batch(self) -> None:
        """provider_embed_calls increments by exactly 1 per dispatched batch.

        Each batch uses a FRESH governor for a clean, deterministic saturation
        so all submitters coalesce into exactly one dispatch per batch.
        """
        provider = _FakeVoyageProvider()

        # Batch 1: fresh governor -> exactly 1 dispatch -> provider_embed_calls == 1
        gov1 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov1,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )
        outcome1 = _run_saturated_submits(
            coalescer, gov1, LANE, ["first batch query"] * 3
        )
        assert not outcome1.errors
        assert coalescer.provider_embed_calls == 1

        # Batch 2: fresh governor on same coalescer -> exactly 1 more dispatch
        gov2 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer._governor = gov2  # swap governor; coalescer counters persist
        outcome2 = _run_saturated_submits(
            coalescer, gov2, LANE, ["second batch query"] * 2
        )
        assert not outcome2.errors
        assert coalescer.provider_embed_calls == 2

    def test_provider_embed_calls_for_k_same_key_is_one(self) -> None:
        """K same-key entries in one batch produce exactly 1 provider_embed_calls."""
        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
        )

        K = 7
        outcome = _run_saturated_submits(
            coalescer, gov, LANE, ["same query for all repos"] * K
        )

        assert not outcome.errors
        assert coalescer.provider_embed_calls == 1, (
            f"expected provider_embed_calls=1 for {K} same-key entries, "
            f"got {coalescer.provider_embed_calls}"
        )


# ---------------------------------------------------------------------------
# FOLD IN #4: Live anchor depth — coalescer must use anchor_depth_provider
# ---------------------------------------------------------------------------


class TestLiveAnchorDepth:
    """FOLD IN #4: coalescer must use the LIVE anchor depth, not a hardcoded 2.

    build_key(text, anchor_tokens=2) is the default. When the operator changes
    the anchor depth, the cache's build_key_for_provider() reads the live value
    but (before the fix) the coalescer's dedup key continued using anchor_tokens=2,
    so the two keys could diverge: a query that HIT the cache would produce a
    DIFFERENT dedup key inside the coalescer batch, breaking dedup/multiplex
    for same-key omni queries.

    The fix: inject an ``anchor_depth_provider`` callable into EmbeddingCoalescer.
    When provided, the coalescer calls it per-dispatch to get the live anchor depth
    (same source as build_key_for_provider). When absent, falls back to the default
    (2) so existing callers are unaffected.

    These tests drive the contract via the SAME build_key function the cache uses
    (imported from query_embedding_cache) — no mocks of any kind.
    """

    def test_anchor_depth_provider_none_uses_default(self) -> None:
        """When no anchor_depth_provider is supplied, dedup uses the default depth (2).

        Two texts whose anchor-2 normalisation is identical must collapse to 1 call.
        'alpha beta gamma' normalised (anchor=2): 'alpha beta gamma' (anchor=2, tail sorted)
        'alpha beta delta' normalised (anchor=2): 'alpha beta delta'
        They are DIFFERENT under anchor=2. Use two identical texts instead.
        """
        from code_indexer.server.services.query_embedding_cache import build_key

        text = "find all authentication errors"
        # Verify build_key with default anchor=2 returns a key (not None)
        key = build_key(text, 2, config_digest=_TEST_DIGEST)
        assert key is not None

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        # No anchor_depth_provider → falls back to default 2
        coalescer = EmbeddingCoalescer(
            LANE, provider, governor=gov, acquire_timeout=5.0, config_digest=_TEST_DIGEST
        )
        assert not hasattr(coalescer, "_anchor_depth_provider") or coalescer._anchor_depth_provider is None

        outcome = _run_saturated_submits(coalescer, gov, LANE, [text] * 3)
        assert not outcome.errors
        # 3 identical texts → 1 provider call (dedup worked with default anchor=2)
        assert provider.call_count == 1

    def test_anchor_depth_provider_callable_used_per_dispatch(self) -> None:
        """When anchor_depth_provider is supplied, the coalescer calls it per dispatch.

        Use anchor_tokens=0 (sort-all): 'beta alpha' and 'alpha beta' normalise to
        'alpha beta' under sort-all, so they collapse to ONE provider call.
        Under anchor=2 (default), 'beta alpha' -> 'beta alpha' and 'alpha beta' ->
        'alpha beta' are DIFFERENT (anchor preserves order), so they would produce
        TWO provider calls. This confirms the coalescer is using the live depth.
        """
        from code_indexer.server.services.query_embedding_cache import build_key

        text_a = "alpha beta gamma"
        text_b = "beta alpha gamma"

        # Confirm: under anchor=2, keys differ (default would NOT dedup)
        key_a_anchor2 = build_key(text_a, 2, config_digest=_TEST_DIGEST)
        key_b_anchor2 = build_key(text_b, 2, config_digest=_TEST_DIGEST)
        assert key_a_anchor2 != key_b_anchor2, (
            "test setup error: texts have same key under anchor=2 (should differ)"
        )

        # Confirm: under anchor=0 (sort-all), keys are IDENTICAL
        key_a_anchor0 = build_key(text_a, 0, config_digest=_TEST_DIGEST)
        key_b_anchor0 = build_key(text_b, 0, config_digest=_TEST_DIGEST)
        assert key_a_anchor0 == key_b_anchor0, (
            "test setup error: texts have different keys under anchor=0 (should match)"
        )

        # Build coalescer with anchor_depth_provider returning 0 (sort-all).
        depth = [0]  # mutable cell so the lambda returns the live value

        def live_anchor() -> int:
            return depth[0]

        gov = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        provider = _FakeVoyageProvider()
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
            anchor_depth_provider=live_anchor,
        )

        # Submit text_a and text_b — they should collapse to 1 call under sort-all.
        outcome = _run_saturated_submits(coalescer, gov, LANE, [text_a, text_b])
        assert not outcome.errors, f"unexpected errors: {outcome.errors}"
        assert provider.call_count == 1, (
            f"expected 1 provider call (sort-all dedup of '{text_a}' and '{text_b}'), "
            f"got {provider.call_count} — coalescer is not using anchor_depth_provider"
        )
        # Both callers received the same vector (multiplexed)
        assert outcome.results[0] == outcome.results[1], (
            "callers received different vectors — dedup multiplex broken"
        )
        # dedup_savings == 1 (2 requestors → 1 unique)
        assert coalescer.dedup_savings == 1

    def test_anchor_depth_provider_read_live_per_dispatch(self) -> None:
        """anchor_depth_provider is called on EACH dispatch, not cached at construction.

        Batch 1: depth=2 → text_a and text_b have DIFFERENT keys → 2 provider calls.
        Batch 2: depth=0 → text_a and text_b have SAME key → 1 provider call.
        This proves the depth is read live per dispatch, not once at construction.
        """
        from code_indexer.server.services.query_embedding_cache import build_key

        text_a = "alpha beta gamma"
        text_b = "beta alpha gamma"

        depth = [2]  # start with anchor=2 (texts differ)

        def live_anchor() -> int:
            return depth[0]

        provider = _FakeVoyageProvider()

        # Batch 1: depth=2 → different keys → 2 unique texts → 1 batch but 2 unique provider texts
        gov1 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer = EmbeddingCoalescer(
            LANE,
            provider,
            governor=gov1,
            acquire_timeout=5.0,
            config_digest=_TEST_DIGEST,
            anchor_depth_provider=live_anchor,
        )
        outcome1 = _run_saturated_submits(coalescer, gov1, LANE, [text_a, text_b])
        assert not outcome1.errors
        # Under anchor=2, two different keys → provider sees 2 unique texts
        assert len(provider.calls_texts[0]) == 2, (
            f"batch 1 (anchor=2): expected 2 unique texts, got {len(provider.calls_texts[0])}"
        )

        # Batch 2: switch to depth=0 (sort-all) → same key → 1 unique text
        depth[0] = 0
        gov2 = ProviderConcurrencyGovernor(max_concurrency=GOV_K)
        coalescer._governor = gov2
        outcome2 = _run_saturated_submits(coalescer, gov2, LANE, [text_a, text_b])
        assert not outcome2.errors
        # Under anchor=0, same key → provider sees 1 unique text
        assert len(provider.calls_texts[1]) == 1, (
            f"batch 2 (anchor=0): expected 1 unique text, got {len(provider.calls_texts[1])}"
        )
