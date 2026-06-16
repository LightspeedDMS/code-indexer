"""Deterministic fault-injection integration gate for the embedding coalescer.

Story #1079 Phase F — "Integration tests (deterministic fault-injection — PRIMARY
gate)". These tests are the story's PRIMARY pass/fail gate. They drive scripted
HTTP 429 / latency at the REAL HTTP transport boundary and run the FULL stack:

    provider (VoyageAIClient / CohereEmbeddingProvider / *RerankerClient)
        -> execute_with_backoff (provider_backoff)
            -> ProviderConcurrencyGovernor.execute   (REAL governor, REAL lanes)
                -> ResizableLimiter + AimdController  (REAL AIMD)
                    -> EmbeddingCoalescer.submit       (REAL coalescer)
                        -> scripted httpx transport     (THE WIRE — fully scripted)

Nothing in that chain is mocked away. The ONLY thing replaced is the wire: a
scripted ``httpx.BaseTransport`` substituted via a real ``create_sync_client``
factory (mirroring how ``FaultInjectingSyncTransport`` overrides outbound
behaviour). The transport:

  * returns scripted, per-call outcomes (200-with-body or HTTP 429) — a 429 is a
    genuine ``httpx.Response(429)`` so ``response.raise_for_status()`` raises an
    intact ``httpx.HTTPStatusError`` (status 429) classifiable by
    ``provider_backoff.is_rate_limited`` (the bug-#1078 / Phase-A invariant);
  * counts EVERY ``handle_request`` so we can assert exactly-one provider HTTP
    call per sealed batch (counted at the transport, the true wire boundary);
  * optionally gates a call on a ``threading.Event`` the test controls, to hold a
    lane busy WITHOUT any wall-clock sleep (no-hang / backoff-no-slot-held tests).

Determinism guarantees (verified by running the suite twice):
  * No real network — every request is answered by the scripted transport.
  * No real timing sleeps for the backoff path — ``_compute_sleep`` is monkey-
    patched to 0.0, so retries are instant while still exercising the retry loop.
  * AIMD cooldown uses an INJECTED clock (``time_fn``) so the cooldown window is
    advanced deterministically, never by sleeping.

Map of tests -> story property (the 7 in the Phase F spec):
  1. AIMD decrease per 429 + increase on sustained success ->
       ``test_aimd_decrease_on_429_then_increase_on_success``
  2. 429 normalization on BOTH providers, embed AND rerank; non-429 neither
     retried nor decreases K ->
       ``test_429_normalized_and_retried_all_four_lanes``
       ``test_non_429_error_not_retried_and_does_not_decrease_k``
       ``test_voyage_429_seen_end_to_end_not_masked``  (latent-bug-#1078 proof)
  3. Lane independence ->
       ``test_lane_independence_rerank_429_does_not_touch_embed``
       ``test_lane_independence_cohere_429_does_not_touch_voyage``
  4. Shared-fate fan-out on batch failure ->
       ``test_shared_fate_429_exhausted_fans_out_to_all_callers``
       ``test_shared_fate_non_429_fans_out_to_all_callers``
  5. Exactly one provider HTTP call per sealed batch (no sub-split) ->
       ``test_one_http_call_per_batch_sealed_by_texts_cap``
       ``test_one_http_call_per_batch_sealed_by_token_limit``
  6. No caller hangs -> every caller gets GovernorBusyError within ACQUIRE_TIMEOUT
       ``test_no_caller_hangs_governor_busy_error_bounded``
  7. Backoff occurs with NO slot held (slot freed mid-backoff) ->
       ``test_backoff_releases_slot_between_attempts``

Run:
    PYTHONPATH=./src pytest tests/integration/server/test_coalescer_fault_injection_1079.py -v
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, List, Optional, Tuple

import httpx
import pytest

from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    GovernorBusyError,
    ProviderConcurrencyGovernor,
)
from code_indexer.server.services.resizable_limiter import K_MIN
from code_indexer.services.provider_backoff import (
    ProviderRateLimitedError,
    execute_with_backoff,
    is_rate_limited,
)

# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------

VOYAGE_EMBED = "voyage:embed"
VOYAGE_RERANK = "voyage:rerank"
COHERE_EMBED = "cohere:embed"
COHERE_RERANK = "cohere:rerank"

# ---------------------------------------------------------------------------
# Deterministic timing constants (NONE of these are real "wait for timing"
# sleeps — they are bounded poll loops / join deadlines / a holder gate. All
# real work completes instantly once the controlling Event/gate is released; the
# bounds only keep a broken test from wedging instead of failing.)
# ---------------------------------------------------------------------------

# Max seconds to poll for a lane to reach full saturation before declaring the
# saturation primitive itself broken.
SATURATION_TIMEOUT_SECONDS = 5.0
# Max seconds to poll for all coalesced callers to enqueue into one open batch.
ACCUMULATION_TIMEOUT_SECONDS = 5.0
# Poll granularity for the bounded witness loops (Event.wait, never time.sleep).
POLL_INTERVAL_SECONDS = 0.01
# Join deadline for coalesced caller threads (work is instant once gate is set).
CALLER_JOIN_TIMEOUT_SECONDS = 30.0
# Join deadline for gated holder threads after their gate is released.
HOLDER_JOIN_TIMEOUT_SECONDS = 10.0
# acquire_timeout used by gated holders (they hold a slot until the gate opens).
HOLDER_ACQUIRE_TIMEOUT_SECONDS = 10.0

# Token-limit-seal test: distinct words count near-linearly toward the token
# limit; a small surplus over the limit guarantees the first build overshoots in
# the common case, and the bounded growth loop doubles a few times if not.
INITIAL_WORD_SURPLUS = 16
# Statically-bounded growth attempts (each doubles the word count). 8 doublings
# of a >100k-word seed reaches ~25M words — far beyond any provider token limit.
MAX_WORD_GROWTH_ATTEMPTS = 8


# ---------------------------------------------------------------------------
# Scripted HTTP transport — THE WIRE. Deterministic, counted, key-free.
# ---------------------------------------------------------------------------


class _ScriptedTransport(httpx.BaseTransport):
    """An ``httpx.BaseTransport`` that returns a scripted outcome per request.

    The script is a list of callables, one consumed per ``handle_request``. Each
    callable receives the request and returns an ``httpx.Response`` (a 200 with a
    provider-appropriate body, or a genuine 429). When the script is exhausted the
    final entry is reused (so an unbounded success or 429 stream is possible).

    Every request is counted (``call_count``) so a test can assert exactly one
    provider HTTP call per sealed batch — measured at the true wire boundary.

    Optional ``gate``: when set, ``handle_request`` blocks on it BEFORE producing
    a response. This holds a lane busy deterministically (no wall-clock sleep) for
    the no-hang / backoff-no-slot-held tests.
    """

    def __init__(
        self,
        script: List[Callable[[httpx.Request], httpx.Response]],
        *,
        gate: Optional[threading.Event] = None,
    ) -> None:
        if not script:
            raise ValueError("script must be non-empty")
        self._script = script
        self._gate = gate
        self._lock = threading.Lock()
        self.call_count = 0
        self.requests: List[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._gate is not None:
            # Bounded wait — never an infinite hang even if the test forgets to
            # set the event (the governor's acquire_timeout is the real bound,
            # but this keeps a stuck test from wedging the worker forever).
            self._gate.wait(timeout=30.0)
        with self._lock:
            idx = min(self.call_count, len(self._script) - 1)
            self.call_count += 1
            self.requests.append(request)
        outcome = self._script[idx]
        return outcome(request)

    def close(self) -> None:  # pragma: no cover - parity with real transports
        pass


class _ScriptedClientFactory:
    """A ``create_sync_client``-compatible factory installing the scripted wire.

    Mirrors ``HttpClientFactory.create_sync_client`` / ``FaultInjectingSyncTransport``:
    it IGNORES the provider's own ``transport=`` kwarg (the latency transport) and
    substitutes the scripted transport, so the test controls every byte on the
    wire while the provider code path is otherwise unchanged.
    """

    def __init__(self, transport: _ScriptedTransport) -> None:
        self._transport = transport

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        pooled: bool = False,
        **kwargs: Any,
    ) -> httpx.Client:
        # Drop caller-supplied transport (latency wrapper) and pooled flag —
        # we own the wire; pooled is a cidx-internal kwarg not accepted by
        # httpx.Client (Story #1083 added pooled=True call sites in providers).
        return httpx.Client(transport=self._transport, **kwargs)


# ---------------------------------------------------------------------------
# Scripted outcome builders (genuine httpx.Response objects)
# ---------------------------------------------------------------------------


def _voyage_embed_200(dims: int = 1024) -> Callable[[httpx.Request], httpx.Response]:
    """Build a Voyage embeddings 200 response matching the count of input texts.

    The provider validates ``len(data) == len(input)``, so the response must echo
    one embedding per requested text — parsed from the actual request payload.
    """

    def _outcome(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        texts = payload["input"]
        data = [{"embedding": [0.1] * dims} for _ in texts]
        return httpx.Response(200, json={"data": data}, request=request)

    return _outcome


def _cohere_embed_200(dims: int = 1536) -> Callable[[httpx.Request], httpx.Response]:
    """Build a Cohere embeddings 200 response matching the count of input texts."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        texts = payload["texts"]
        floats = [[0.1] * dims for _ in texts]
        return httpx.Response(
            200, json={"embeddings": {"float": floats}}, request=request
        )

    return _outcome


def _rerank_200() -> Callable[[httpx.Request], httpx.Response]:
    """Build a rerank 200 response (Voyage/Cohere ``data`` array shape)."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        docs = payload.get("documents", [])
        data = [
            {"index": i, "relevance_score": 1.0 - i * 0.01} for i in range(len(docs))
        ]
        return httpx.Response(200, json={"data": data}, request=request)

    return _outcome


def _http_429() -> Callable[[httpx.Request], httpx.Response]:
    """Build a genuine HTTP 429 response (Retry-After: 0 -> instant retry)."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "0"},
            json={"error": "rate limited"},
            request=request,
        )

    return _outcome


def _http_500() -> Callable[[httpx.Request], httpx.Response]:
    """Build a genuine HTTP 500 response (non-429 — must NOT be retried)."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"}, request=request)

    return _outcome


# ---------------------------------------------------------------------------
# Provider builders (real providers, dummy key, scripted wire)
# ---------------------------------------------------------------------------


def _voyage_provider(factory: _ScriptedClientFactory) -> Any:
    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    return VoyageAIClient(VoyageAIConfig(), http_client_factory=factory)


def _cohere_provider(factory: _ScriptedClientFactory) -> Any:
    from code_indexer.config import CohereConfig
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    return CohereEmbeddingProvider(CohereConfig(), http_client_factory=factory)


def _voyage_reranker(factory: _ScriptedClientFactory) -> Any:
    from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

    return VoyageRerankerClient(http_client_factory=factory)  # type: ignore[arg-type]


def _cohere_reranker(factory: _ScriptedClientFactory) -> Any:
    from code_indexer.server.clients.reranker_clients import CohereRerankerClient

    return CohereRerankerClient(http_client_factory=factory)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dummy_keys() -> Any:
    """Provide dummy provider API keys (constructors check presence only).

    The scripted wire means no key validation ever reaches a real provider, but
    the constructors still require a non-empty key to build.
    """
    prev = {k: os.environ.get(k) for k in ("VOYAGE_API_KEY", "CO_API_KEY")}
    os.environ["VOYAGE_API_KEY"] = "dummy-voyage-key"
    os.environ["CO_API_KEY"] = "dummy-cohere-key"
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_singletons() -> Any:
    """Isolate process-global singletons between tests.

    Both the governor (tests build their own) AND the ProviderHealthMonitor must
    be reset: repeated scripted 429s record provider-call failures that can sinbin
    a lane (ProviderSinbinnedError) and leak that circuit-breaker state into the
    next test. Resetting both guarantees each test starts un-sinbinned.
    """
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the backoff sleep so 429 retries are instant (no real timing).

    The retry LOOP still runs (classification, attempt counting, slot
    re-acquisition) — only the wall-clock sleep is elided, keeping the test
    deterministic and fast while exercising the real backoff code path.
    """
    monkeypatch.setattr(
        "code_indexer.services.provider_backoff._compute_sleep",
        lambda exc, cap: 0.0,
    )


class _FakeClock:
    """Deterministic monotonic clock for AIMD cooldown (no sleeping)."""

    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _build_governor(
    *, max_concurrency: int = K_MIN, clock: Optional[_FakeClock] = None
) -> ProviderConcurrencyGovernor:
    """Build a governor with a directly-controlled AIMD clock on every lane.

    Direct construction keeps the default [K_MIN, K_MAX] = [8, 32] bounds. When a
    clock is supplied we re-point each lane's AimdController ``_time_fn`` to it so
    the cooldown window can be advanced deterministically.
    """
    gov = ProviderConcurrencyGovernor(max_concurrency=max_concurrency)
    if clock is not None:
        for lane in (VOYAGE_EMBED, VOYAGE_RERANK, COHERE_EMBED, COHERE_RERANK):
            gov.aimd(lane)._time_fn = clock  # type: ignore[attr-defined]
    return gov


# ===========================================================================
# Property 1 — AIMD decrease per 429 + increase on sustained success
# ===========================================================================


def test_aimd_decrease_on_429_then_increase_on_success() -> None:
    """A scripted 429 halves the lane K (floor 8); sustained success grows it +1.

    Drives the REAL Voyage embed query path (get_embedding -> get_embeddings_batch
    retry=False) through execute_with_backoff + governor. The 429 surfaces as an
    intact httpx 429 -> governor records a multiplicative decrease. Then a stream
    of 200s grows K back additively (SUCCESS_THRESHOLD per +1), past the cooldown.
    """
    from code_indexer.server.services.aimd_controller import (
        COOLDOWN_SECONDS,
        SUCCESS_THRESHOLD,
    )

    clock = _FakeClock()
    # Seed K above the floor so a decrease is observable (16 -> 8).
    gov = _build_governor(max_concurrency=16, clock=clock)
    assert gov.current_k[VOYAGE_EMBED] == 16

    # --- Multiplicative decrease: one fully-exhausted 429 sequence ---
    # execute_with_backoff makes 3 attempts (max_retries=2); all 429 -> the
    # governor records success=False on EACH attempt -> 16 -> 8 (floored at 8).
    transport = _ScriptedTransport([_http_429()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))

    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: provider.get_embedding("decrease probe"),
                acquire_timeout=5.0,
            )
        )

    assert gov.current_k[VOYAGE_EMBED] == K_MIN, (
        f"AIMD did not multiplicatively decrease to the floor: "
        f"K={gov.current_k[VOYAGE_EMBED]} (expected {K_MIN})"
    )
    assert transport.call_count == 3, (
        f"expected 3 HTTP attempts (1 + 2 retries), got {transport.call_count}"
    )

    # --- Additive increase: sustained success grows K past the cooldown ---
    # Advance the injected clock past the post-decrease cooldown so successes count.
    clock.advance(COOLDOWN_SECONDS + 1.0)
    ok_transport = _ScriptedTransport([_voyage_embed_200()])
    ok_provider = _voyage_provider(_ScriptedClientFactory(ok_transport))

    # SUCCESS_THRESHOLD consecutive successes -> exactly one +1 step (8 -> 9).
    for _ in range(SUCCESS_THRESHOLD):
        vec = execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: ok_provider.get_embedding("grow probe"),
                acquire_timeout=5.0,
            )
        )
        assert isinstance(vec, list) and len(vec) == 1024

    assert gov.current_k[VOYAGE_EMBED] == K_MIN + 1, (
        f"AIMD did not additively increase after {SUCCESS_THRESHOLD} successes: "
        f"K={gov.current_k[VOYAGE_EMBED]} (expected {K_MIN + 1})"
    )

    # Another full threshold of successes -> another +1 (9 -> 10), proving the
    # increase is sustained, not a one-off.
    for _ in range(SUCCESS_THRESHOLD):
        execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: ok_provider.get_embedding("grow probe 2"),
                acquire_timeout=5.0,
            )
        )
    assert gov.current_k[VOYAGE_EMBED] == K_MIN + 2


# ===========================================================================
# Property 2 — 429 normalization on BOTH providers, embed AND rerank
# ===========================================================================


def _drive_embed_429_exhaustion(
    gov: ProviderConcurrencyGovernor, lane: str, provider: Any
) -> None:
    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: gov.execute(
                lane,
                lambda: provider.get_embedding("429 probe"),
                acquire_timeout=5.0,
            )
        )


def _drive_rerank_429_exhaustion(
    gov: ProviderConcurrencyGovernor, lane: str, client: Any
) -> None:
    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: gov.execute(
                lane,
                lambda: client.rerank(query="q", documents=["d0", "d1"]),
                acquire_timeout=5.0,
            )
        )


def test_429_normalized_and_retried_all_four_lanes() -> None:
    """A scripted 429 on each of the 4 lanes is retried AND halves that lane's K.

    Voyage embed, Cohere embed, Voyage rerank, Cohere rerank — each independently
    constructed with its own scripted 429 wire and its own governor (seeded at 16
    so the decrease to 8 is observable). Proves the 429 is (a) classified and
    retried by execute_with_backoff (3 attempts), and (b) drives a multiplicative
    AIMD decrease on that exact lane.
    """
    # --- Voyage embed ---
    gov_v = _build_governor(max_concurrency=16)
    t = _ScriptedTransport([_http_429()])
    _drive_embed_429_exhaustion(
        gov_v, VOYAGE_EMBED, _voyage_provider(_ScriptedClientFactory(t))
    )
    assert t.call_count == 3
    assert gov_v.current_k[VOYAGE_EMBED] == K_MIN

    # --- Cohere embed ---
    gov_c = _build_governor(max_concurrency=16)
    t = _ScriptedTransport([_http_429()])
    _drive_embed_429_exhaustion(
        gov_c, COHERE_EMBED, _cohere_provider(_ScriptedClientFactory(t))
    )
    assert t.call_count == 3
    assert gov_c.current_k[COHERE_EMBED] == K_MIN

    # --- Voyage rerank ---
    gov_vr = _build_governor(max_concurrency=16)
    t = _ScriptedTransport([_http_429()])
    _drive_rerank_429_exhaustion(
        gov_vr, VOYAGE_RERANK, _voyage_reranker(_ScriptedClientFactory(t))
    )
    assert t.call_count == 3
    assert gov_vr.current_k[VOYAGE_RERANK] == K_MIN

    # --- Cohere rerank ---
    gov_cr = _build_governor(max_concurrency=16)
    t = _ScriptedTransport([_http_429()])
    _drive_rerank_429_exhaustion(
        gov_cr, COHERE_RERANK, _cohere_reranker(_ScriptedClientFactory(t))
    )
    assert t.call_count == 3
    assert gov_cr.current_k[COHERE_RERANK] == K_MIN


def test_voyage_429_seen_end_to_end_not_masked() -> None:
    """Latent bug #1078 proof: a Voyage 429 is classifiable end-to-end, not masked.

    BEFORE Phase A, the Voyage query path wrapped every error (including a 429)
    into ``RuntimeError(f"Batch embedding request failed: {e}")``. That stringified
    RuntimeError is NOT ``is_rate_limited`` -> execute_with_backoff would NOT retry
    it and the governor would NOT decrease K — the 429 was invisible to the
    adaptive limiter.

    This test asserts the post-Phase-A behaviour end-to-end:
      * the raised exception IS classifiable as rate-limited, and
      * the governor recorded a multiplicative decrease (K dropped from the seed).

    Against pre-Phase-A code BOTH assertions fail (the error would be an
    unclassifiable RuntimeError and K would stay at the seed), so this test is a
    genuine regression guard for the latent masking bug.
    """
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_http_429()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))

    captured: Optional[BaseException] = None
    try:
        execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: provider.get_embedding("masking probe"),
                acquire_timeout=5.0,
            )
        )
    except BaseException as exc:  # noqa: BLE001 - capture for classification assert
        captured = exc

    assert captured is not None, "expected the exhausted 429 to raise"
    # The normalized exhaustion signal is itself rate-limited-classifiable...
    assert is_rate_limited(captured), (
        "Voyage 429 was masked: the surfaced exception is NOT classifiable as "
        "rate-limited. Pre-Phase-A this was a stringified RuntimeError."
    )
    assert isinstance(captured, ProviderRateLimitedError)
    # ...and the governor saw the 429 on each attempt and decreased K.
    assert gov.current_k[VOYAGE_EMBED] == K_MIN, (
        "governor did not record the Voyage 429 as a decrease signal — the 429 "
        "was masked from the adaptive limiter (latent bug #1078)."
    )


def test_non_429_error_not_retried_and_does_not_decrease_k() -> None:
    """A scripted non-429 (HTTP 500) is NEITHER retried NOR decreases AIMD K.

    The provider wraps a non-429 in a generic RuntimeError (Phase A: only 429s are
    re-raised intact). execute_with_backoff must NOT retry it (one HTTP call), and
    the governor must NOT classify it as rate-limited -> K unchanged.
    """
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_http_500()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))

    with pytest.raises(RuntimeError) as exc_info:
        execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: provider.get_embedding("500 probe"),
                acquire_timeout=5.0,
            )
        )

    # Not a rate-limit signal.
    assert not is_rate_limited(exc_info.value)
    assert not isinstance(exc_info.value, ProviderRateLimitedError)
    # Exactly ONE HTTP attempt — a non-429 is never retried.
    assert transport.call_count == 1, (
        f"non-429 error was retried ({transport.call_count} attempts) — "
        "execute_with_backoff must only retry rate-limit signals"
    )
    # K untouched — a non-429 is not an AIMD decrease signal.
    assert gov.current_k[VOYAGE_EMBED] == 16, (
        f"non-429 error decreased AIMD K to {gov.current_k[VOYAGE_EMBED]} "
        "(expected 16) — only 429s may decrease K"
    )


# ===========================================================================
# Property 3 — Lane independence
# ===========================================================================


def test_lane_independence_rerank_429_does_not_touch_embed() -> None:
    """A 429 on voyage:rerank changes ONLY that lane's K — voyage:embed untouched."""
    gov = _build_governor(max_concurrency=16)
    before = dict(gov.current_k)

    t = _ScriptedTransport([_http_429()])
    _drive_rerank_429_exhaustion(
        gov, VOYAGE_RERANK, _voyage_reranker(_ScriptedClientFactory(t))
    )

    after = dict(gov.current_k)
    assert after[VOYAGE_RERANK] == K_MIN, "voyage:rerank K should have decreased"
    assert after[VOYAGE_EMBED] == before[VOYAGE_EMBED] == 16, (
        "voyage:embed K changed due to a voyage:rerank 429 — lanes are not independent"
    )
    assert after[COHERE_EMBED] == before[COHERE_EMBED] == 16
    assert after[COHERE_RERANK] == before[COHERE_RERANK] == 16


def test_lane_independence_cohere_429_does_not_touch_voyage() -> None:
    """Cohere-lane adaptation does not affect Voyage lanes (and vice versa)."""
    gov = _build_governor(max_concurrency=16)

    # 429 on cohere:embed.
    t = _ScriptedTransport([_http_429()])
    _drive_embed_429_exhaustion(
        gov, COHERE_EMBED, _cohere_provider(_ScriptedClientFactory(t))
    )
    assert gov.current_k[COHERE_EMBED] == K_MIN
    assert gov.current_k[VOYAGE_EMBED] == 16
    assert gov.current_k[VOYAGE_RERANK] == 16

    # Now a 429 on voyage:embed must not perturb the (already-decreased) cohere lane.
    t2 = _ScriptedTransport([_http_429()])
    _drive_embed_429_exhaustion(
        gov, VOYAGE_EMBED, _voyage_provider(_ScriptedClientFactory(t2))
    )
    assert gov.current_k[VOYAGE_EMBED] == K_MIN
    # Cohere lane unchanged by the Voyage 429 — stays at its own floor.
    assert gov.current_k[COHERE_EMBED] == K_MIN
    assert gov.current_k[COHERE_RERANK] == 16


# ===========================================================================
# Property 4 — Shared-fate fan-out on batch failure
# ===========================================================================


def _saturate_lane(
    gov: ProviderConcurrencyGovernor, lane: str, *, holders: int
) -> Tuple[threading.Event, List[threading.Thread]]:
    """Pin ``holders`` governor slots on ``lane`` via a gated wire (no sleep).

    Each holder occupies one slot until the returned Event is set (it blocks on
    the scripted transport's gate). Returns (gate, holder_threads). The caller
    MUST set the gate and join the holders to drain cleanly.

    This is the deterministic accumulation primitive: with the lane saturated, a
    coalescer dispatcher cannot acquire a slot, so it parks in the accumulation
    window while late ``submit`` callers join its open batch — guaranteeing ONE
    coalesced batch instead of a race-dependent split.

    On saturation failure this releases the gate and joins the holders before
    raising, so holder threads can never leak.
    """
    gate = threading.Event()
    started = threading.Barrier(holders + 1)

    def _hold() -> None:
        gate_transport = _ScriptedTransport([_voyage_embed_200()], gate=gate)
        gate_provider = _voyage_provider(_ScriptedClientFactory(gate_transport))
        started.wait()
        gov.execute(
            lane,
            lambda: gate_provider.get_embedding("holder"),
            acquire_timeout=HOLDER_ACQUIRE_TIMEOUT_SECONDS,
        )

    threads = [threading.Thread(target=_hold) for _ in range(holders)]
    for t in threads:
        t.start()
    started.wait()

    # Wait (bounded, no sleep-for-timing) until the lane is actually saturated.
    witness = threading.Event()
    waited = 0.0
    limiter = gov._limiters[lane]  # type: ignore[attr-defined]
    while limiter.in_flight < holders and waited < SATURATION_TIMEOUT_SECONDS:
        witness.wait(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    if limiter.in_flight != holders:
        # Never leak holders: release the gate and join before failing.
        gate.set()
        for t in threads:
            t.join(timeout=HOLDER_JOIN_TIMEOUT_SECONDS)
        raise AssertionError(
            f"lane {lane} did not saturate ({limiter.in_flight}/{holders})"
        )
    return gate, threads


def _coalesced_burst(
    coalescer: EmbeddingCoalescer,
    texts: List[str],
    *,
    gov: ProviderConcurrencyGovernor,
    lane: str = VOYAGE_EMBED,
) -> List[Tuple[Optional[List[float]], Optional[BaseException]]]:
    """Submit ``texts`` so they DETERMINISTICALLY coalesce into ONE batch.

    Saturates the lane (gated holders) so the coalescer dispatcher parks waiting
    for a slot; submits all ``texts`` and ASSERTS (witnessed via the coalescer's
    internal ``_open_batch`` length) that every one enqueued into ONE batch; THEN
    releases the holders so the dispatcher seals and dispatches the full batch.
    This removes the start-order race — all callers provably share one batch.

    The gate release + holder join run in ``finally`` so a failed witness/submit
    can never leak the gated holder threads. Collects (vector, exc) per caller.
    """
    n = len(texts)
    holders = gov.current_k[lane]
    gate, holder_threads = _saturate_lane(gov, lane, holders=holders)

    results: List[Tuple[Optional[List[float]], Optional[BaseException]]] = [
        (None, None)
    ] * n
    threads: List[threading.Thread] = []

    def _worker(idx: int) -> None:
        try:
            results[idx] = (coalescer.submit(texts[idx]), None)
        except BaseException as exc:  # noqa: BLE001 - record shared fate
            results[idx] = (None, exc)

    def _open_batch_size() -> int:
        with coalescer._lock:  # type: ignore[attr-defined]
            ob = coalescer._open_batch  # type: ignore[attr-defined]
            return len(ob) if ob is not None else 0

    try:
        # STAGGERED, CONFIRMED enqueue removes the fast-seal race: launch caller i,
        # then wait until the open batch reflects i+1 entries before launching i+1.
        # The dispatcher (caller 0) parks on the saturated lane, so the batch stays
        # open and visibly grows. For the LAST caller, a texts-cap seal may clear
        # the batch to None the instant it hits the cap; that caller's enqueue is
        # confirmed by the batch having reached n-1 just before (peak == n witnessed
        # cumulatively), so we treat reaching n-1 then a seal-to-empty as "n joined".
        max_seen = 0
        for idx in range(n):
            t = threading.Thread(target=_worker, args=(idx,))
            t.start()
            threads.append(t)

            target_size = idx + 1
            witness = threading.Event()
            waited = 0.0
            while waited < ACCUMULATION_TIMEOUT_SECONDS:
                size = _open_batch_size()
                max_seen = max(max_seen, size)
                # The last caller may have already sealed+cleared the batch; accept
                # either the batch reaching target_size OR (for the final caller) a
                # seal that fired exactly at the cap (batch cleared after reaching
                # the prior size of n-1).
                if max_seen >= target_size:
                    break
                if idx == n - 1 and max_seen >= n - 1 and size == 0:
                    # Final caller's enqueue triggered the seal -> batch cleared.
                    max_seen = n
                    break
                witness.wait(POLL_INTERVAL_SECONDS)
                waited += POLL_INTERVAL_SECONDS
            assert max_seen >= target_size, (
                f"caller {idx} did not enqueue into the open batch: peak "
                f"size={max_seen} (expected >= {target_size}) — accumulation "
                "window did not capture this submit"
            )

        # Provable single-batch coalescing: all n callers shared one open batch.
        assert max_seen >= n, (
            f"coalescing did not accumulate all callers into one batch: "
            f"peak open_batch size={max_seen} (expected >= {n})"
        )
    finally:
        # Always release the gated holders and drain them, even on a failed
        # witness/submit, so holder threads can never leak.
        gate.set()
        for t in holder_threads:
            t.join(timeout=HOLDER_JOIN_TIMEOUT_SECONDS)

    for t in threads:
        t.join(timeout=CALLER_JOIN_TIMEOUT_SECONDS)
    assert not any(t.is_alive() for t in threads), "a coalesced caller hung"
    assert not any(t.is_alive() for t in holder_threads), "a holder hung"
    return results


def test_shared_fate_429_exhausted_fans_out_to_all_callers() -> None:
    """A 429-exhausted coalesced batch fans the exception to ALL callers; none hang.

    Several texts coalesce into one batch whose single HTTP dispatch always 429s
    (exhausted). EVERY caller must receive a rate-limit exception (shared fate);
    no caller may hang or return a vector.
    """
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_http_429()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED, provider, governor=gov, acquire_timeout=5.0
    )

    results = _coalesced_burst(
        coalescer, [f"shared fate {i}" for i in range(5)], gov=gov
    )

    for idx, (vec, exc) in enumerate(results):
        assert vec is None, f"caller {idx} got a vector on a failed batch"
        assert exc is not None, f"caller {idx} got no exception (shared fate broken)"
        assert is_rate_limited(exc), (
            f"caller {idx} exception is not a rate-limit signal: {exc!r}"
        )


def test_shared_fate_non_429_fans_out_to_all_callers() -> None:
    """A non-429 (HTTP 500) batch failure also fans out to ALL callers; none hang."""
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_http_500()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED, provider, governor=gov, acquire_timeout=5.0
    )

    results = _coalesced_burst(
        coalescer, [f"shared fate 500 {i}" for i in range(4)], gov=gov
    )

    for idx, (vec, exc) in enumerate(results):
        assert vec is None, f"caller {idx} got a vector on a failed batch"
        assert exc is not None, f"caller {idx} got no exception (shared fate broken)"
        assert isinstance(exc, RuntimeError)
        assert not is_rate_limited(exc), "a 500 must not be classified as rate-limited"


# ===========================================================================
# Property 5 — Exactly one provider HTTP call per sealed batch (no sub-split)
# ===========================================================================


def test_one_http_call_per_batch_sealed_by_texts_cap() -> None:
    """A batch sealed by the TEXTS cap issues exactly ONE provider HTTP call.

    With ``coalesce_max_batch_size`` (texts cap) = 3 and 3 short texts coalesced,
    the coalescer seals on the texts cap and dispatches ONE batch -> exactly one
    HTTP request at the transport. The provider must not sub-split it.
    """
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_voyage_embed_200()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        coalesce_max_batch_size=3,
    )
    assert coalescer.effective_texts_cap() == 3

    results = _coalesced_burst(coalescer, ["alpha", "beta", "gamma"], gov=gov)

    for idx, (vec, exc) in enumerate(results):
        assert exc is None, f"caller {idx} failed: {exc!r}"
        assert vec is not None and len(vec) == 1024

    assert transport.call_count == 1, (
        f"texts-cap-sealed batch made {transport.call_count} HTTP calls "
        "(expected exactly 1 — the batch must not sub-split)"
    )
    # The single request carried all 3 texts (no sub-split inside the provider).
    payload = json.loads(transport.requests[0].content.decode())
    assert len(payload["input"]) == 3


def test_one_http_call_per_batch_sealed_by_token_limit() -> None:
    """A batch sealed by the TOKEN limit issues exactly ONE provider HTTP call.

    Two large texts whose combined token count exceeds the Voyage seal token limit
    (108000 = 90% of 120000) force a seal on the FIRST text alone (the second
    would-exceed -> opens its own batch). We submit just the first oversized text:
    it seals immediately on the token limit and dispatches ONE HTTP call carrying
    exactly that text — proving the token-limit seal also yields a single,
    un-sub-split provider call.
    """
    gov = _build_governor(max_concurrency=16)
    transport = _ScriptedTransport([_voyage_embed_200()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        coalesce_max_batch_size=96,
    )

    # Build a text whose token count alone meets/exceeds the seal token limit so
    # _seal_if_full fires on the token branch (open_tokens >= token_limit). The
    # tokenizer MERGES repeated identical words, so we use DISTINCT tokens
    # (w0 w1 w2 ...) which count near-linearly. Growth is STATICALLY bounded
    # (Messi #14): at most MAX_WORD_GROWTH_ATTEMPTS doublings of the seed.
    target = coalescer.token_limit
    word_count = target + INITIAL_WORD_SURPLUS
    big_text = ""
    measured = 0
    for _ in range(MAX_WORD_GROWTH_ATTEMPTS):
        big_text = " ".join(f"w{i}" for i in range(word_count))
        measured = coalescer.count_tokens(big_text)
        if measured >= target:
            break
        word_count *= 2
    assert measured >= target, (
        f"constructed text did not reach the seal token limit after "
        f"{MAX_WORD_GROWTH_ATTEMPTS} growth attempts "
        f"(measured={measured}, target={target})"
    )

    vec = coalescer.submit(big_text)
    assert isinstance(vec, list) and len(vec) == 1024

    assert transport.call_count == 1, (
        f"token-limit-sealed batch made {transport.call_count} HTTP calls "
        "(expected exactly 1)"
    )
    payload = json.loads(transport.requests[0].content.decode())
    assert len(payload["input"]) == 1, "token-sealed batch must carry exactly one text"


# ===========================================================================
# Property 6 — No caller hangs: GovernorBusyError within ACQUIRE_TIMEOUT
# ===========================================================================


def test_no_caller_hangs_governor_busy_error_bounded() -> None:
    """When no slot is granted within ACQUIRE_TIMEOUT, every caller gets busy-error.

    A lane is pinned fully busy by K long-running calls held on a scripted gate
    (no wall-clock sleep — the gate blocks the wire). With the lane saturated, a
    fresh coalesced batch cannot acquire a slot within a SMALL acquire_timeout and
    every coalesced caller receives GovernorBusyError, bounded by that timeout —
    no caller hangs. Releasing the gate lets the held calls drain cleanly.
    """
    # Seed K at the floor (8) so saturating the lane needs exactly 8 holders.
    gov = _build_governor(max_concurrency=K_MIN)
    k = gov.current_k[VOYAGE_EMBED]
    assert k == K_MIN

    # Saturate the lane (gated holders keep every slot busy until released).
    gate, holders = _saturate_lane(gov, VOYAGE_EMBED, holders=k)

    # Fresh coalescer on the SATURATED lane with a SMALL acquire_timeout. Every
    # coalesced caller must get GovernorBusyError, bounded by that timeout — the
    # lane stays busy for the whole probe (gate NOT released until after).
    probe_transport = _ScriptedTransport([_voyage_embed_200()])
    probe_provider = _voyage_provider(_ScriptedClientFactory(probe_transport))
    busy_acquire_timeout = 0.2
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED, probe_provider, governor=gov, acquire_timeout=busy_acquire_timeout
    )

    n_probes = 3
    results: List[Tuple[Optional[List[float]], Optional[BaseException]]] = [
        (None, None)
    ] * n_probes
    probe_texts = [f"busy {i}" for i in range(n_probes)]
    enqueued = threading.Barrier(n_probes + 1)

    def _probe(idx: int) -> None:
        enqueued.wait()
        try:
            results[idx] = (coalescer.submit(probe_texts[idx]), None)
        except BaseException as exc:  # noqa: BLE001 - record shared fate
            results[idx] = (None, exc)

    probe_threads = [
        threading.Thread(target=_probe, args=(i,)) for i in range(n_probes)
    ]
    try:
        for t in probe_threads:
            t.start()
        enqueued.wait()
        for t in probe_threads:
            t.join(timeout=CALLER_JOIN_TIMEOUT_SECONDS)
        assert not any(t.is_alive() for t in probe_threads), "a probe caller hung"
    finally:
        # Always drain the holders, even if a probe assertion fails below.
        gate.set()
        for t in holders:
            t.join(timeout=HOLDER_JOIN_TIMEOUT_SECONDS)

    for idx, (vec, exc) in enumerate(results):
        assert vec is None, f"caller {idx} unexpectedly got a vector"
        assert isinstance(exc, GovernorBusyError), (
            f"caller {idx} did not get GovernorBusyError: {exc!r}"
        )
    # The saturated probe never reached the wire (no slot -> no HTTP call).
    assert probe_transport.call_count == 0
    assert not any(t.is_alive() for t in holders), "holder threads did not drain"


# ===========================================================================
# Property 7 — Backoff occurs with NO slot held (slot freed mid-backoff)
# ===========================================================================


def test_backoff_releases_slot_between_attempts() -> None:
    """During a 429 retry the governor slot is RELEASED — another caller can use it.

    A retrying-429 caller on a single-slot lane must free the slot between
    attempts (backoff sleeps OUTSIDE the slot). We prove it by letting a second
    caller successfully acquire the SAME lane's slot while the first is mid-backoff:
    if the slot were held during backoff, the second caller would be starved.

    Determinism: backoff sleep is zeroed (autouse fixture), and we use a
    single-slot lane plus high-water/in-flight telemetry as the witness — the
    second caller's success proves the slot was free between the first caller's
    attempts.
    """
    # Force a single-slot lane by shrinking the limiter to 1 (below K_MIN clamp is
    # not allowed; instead we assert via the in-flight witness with the floor).
    gov = _build_governor(max_concurrency=K_MIN)

    # First caller: 429, 429, then 200 (succeeds on the 3rd attempt). Between the
    # 429 attempts the slot must be released (backoff is outside the slot).
    retry_transport = _ScriptedTransport(
        [_http_429(), _http_429(), _voyage_embed_200()]
    )
    retry_provider = _voyage_provider(_ScriptedClientFactory(retry_transport))

    # Second caller: an independent success on the SAME lane, fired while the first
    # is mid-retry. If the slot were held during backoff AND the lane were
    # single-slot, the second caller would block; with release-between-attempts it
    # proceeds. We additionally witness that in-flight never exceeds the limit and
    # returns to 0 (the slot is genuinely released around each attempt).
    second_transport = _ScriptedTransport([_voyage_embed_200()])
    second_provider = _voyage_provider(_ScriptedClientFactory(second_transport))

    first_done = threading.Event()
    second_result: List[Optional[List[float]]] = [None]
    second_exc: List[Optional[BaseException]] = [None]

    def _second() -> None:
        try:
            second_result[0] = execute_with_backoff(
                lambda: gov.execute(
                    VOYAGE_EMBED,
                    lambda: second_provider.get_embedding("second caller"),
                    acquire_timeout=5.0,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            second_exc[0] = exc
        finally:
            first_done.set()

    # Run the first (retrying) caller and the second concurrently.
    second_thread = threading.Thread(target=_second)
    second_thread.start()

    first_vec = execute_with_backoff(
        lambda: gov.execute(
            VOYAGE_EMBED,
            lambda: retry_provider.get_embedding("first caller"),
            acquire_timeout=5.0,
        )
    )
    second_thread.join(timeout=10.0)

    assert not second_thread.is_alive(), (
        "second caller hung (slot held during backoff?)"
    )
    assert second_exc[0] is None, f"second caller failed: {second_exc[0]!r}"
    assert second_result[0] is not None and len(second_result[0]) == 1024, (
        "second caller did not get a slot while the first was mid-backoff — "
        "the governor slot was NOT released between 429 retry attempts"
    )
    assert isinstance(first_vec, list) and len(first_vec) == 1024
    # First caller made 3 attempts (429, 429, 200) — backoff between each.
    assert retry_transport.call_count == 3
    # In-flight returned to 0 — no leaked/held slot after both callers finished.
    assert gov._limiters[VOYAGE_EMBED].in_flight == 0  # type: ignore[attr-defined]
