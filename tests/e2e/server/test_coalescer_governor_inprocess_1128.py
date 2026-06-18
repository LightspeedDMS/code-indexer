"""In-process coalescer + governor harness for Story #1128 / Epic #1121.

Phase 3 (tests/e2e/server/) — no TestClient / REST server needed; the governor
and coalescer are pure in-process code with no REST/MCP exposure for ``current_k``.

What is tested and WHY it lives here
-------------------------------------
``governor.current_k`` has NO REST/MCP front-door exposure, so the Phase-5
front-door 429 test (S6a) cannot directly observe K-halving or per-lane
isolation.  This in-process harness fills that gap:

  AC1 — N concurrent submits -> exactly ONE coalesced provider HTTP call.
  AC2 — A 429 halves the faulted lane's K; the other lane is unchanged.
  Mutation — Sharing ONE AimdController across the two :embed lanes detects
              cross-lane K bleed; the correct per-lane governor keeps lanes
              independent.

Design modelled on ``tests/integration/server/test_coalescer_fault_injection_1079.py``:
  - Scripted httpx transport at the provider wire (line ~127 of that file).
  - threading.Barrier/Event for deterministic accumulation (lines ~679-728).
  - Injected fake clock + zeroed backoff for no-sleep determinism (line ~357).

No mocking of the coalescer, governor, or AIMD controller.  The ONLY thing
replaced is the wire: a scripted ``httpx.BaseTransport``.

Determinism guarantee
---------------------
  - No real network — every provider HTTP request answered by the scripted transport.
  - No real timing sleeps — ``_compute_sleep`` monkey-patched to 0.0 so 429
    retries are instant while exercising the real retry loop.
  - AIMD cooldown uses an INJECTED fixed clock (no wall-clock dependency); the
    tests drive K downward only, so it never needs advancing.

Log-audit gate note (Story #1122)
----------------------------------
The AC2 429 test triggers an AIMD multiplicative-decrease WARNING
("AIMD multiplicative decrease").  That pattern is already allowlisted in
``tests/e2e/log_audit_gate.py`` (added by S6a), so no new allowlist entry is
needed.

Run standalone:
    PYTHONPATH=src python3 -m pytest tests/e2e/server/test_coalescer_governor_inprocess_1128.py \\
        -p no:randomly -q -rs
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, List, Optional, Tuple

import httpx
import pytest

from code_indexer.server.services.aimd_controller import AimdController
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)
from code_indexer.server.services.resizable_limiter import (
    K_MAX,
    K_MIN,
    ResizableLimiter,
)
from code_indexer.services.provider_backoff import (
    ProviderRateLimitedError,
    execute_with_backoff,
)

# ---------------------------------------------------------------------------
# Lane name constants
# ---------------------------------------------------------------------------

VOYAGE_EMBED = "voyage:embed"
VOYAGE_RERANK = "voyage:rerank"
COHERE_EMBED = "cohere:embed"
COHERE_RERANK = "cohere:rerank"

# ---------------------------------------------------------------------------
# Deterministic timing constants — NONE are real "wait for timing" sleeps.
# All real work completes once the controlling Event/Barrier is released; the
# bounds only keep a broken test from wedging instead of failing.
# ---------------------------------------------------------------------------

# Max seconds to poll for lane saturation.
SATURATION_TIMEOUT_SECONDS = 5.0
# Max seconds to poll for all callers to enqueue into one open batch.
ACCUMULATION_TIMEOUT_SECONDS = 5.0
# Poll granularity (Event.wait, never time.sleep).
POLL_INTERVAL_SECONDS = 0.01
# Join deadline for coalesced caller threads.
CALLER_JOIN_TIMEOUT_SECONDS = 30.0
# Join deadline for gated holder threads.
HOLDER_JOIN_TIMEOUT_SECONDS = 10.0
# acquire_timeout used by gated holder threads.
HOLDER_ACQUIRE_TIMEOUT_SECONDS = 10.0


# ---------------------------------------------------------------------------
# Scripted HTTP transport (THE WIRE — copied verbatim from 1079 harness)
# ---------------------------------------------------------------------------


class _ScriptedTransport(httpx.BaseTransport):
    """An httpx.BaseTransport that returns a scripted outcome per request.

    Each handle_request consumes one entry from the script list; when exhausted
    the last entry is reused.  call_count counts every request so tests can
    assert exactly-one provider HTTP call per sealed batch.

    Optional ``gate``: when set, handle_request blocks on the Event BEFORE
    producing a response, holding a lane slot deterministically without sleeping.
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
            self._gate.wait(timeout=30.0)
        with self._lock:
            idx = min(self.call_count, len(self._script) - 1)
            self.call_count += 1
            self.requests.append(request)
        return self._script[idx](request)

    def close(self) -> None:  # pragma: no cover
        pass


class _ScriptedClientFactory:
    """A create_sync_client-compatible factory installing the scripted wire."""

    def __init__(self, transport: _ScriptedTransport) -> None:
        self._transport = transport

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        pooled: bool = False,
        **kwargs: Any,
    ) -> httpx.Client:
        # Drop caller-supplied transport and pooled flag — we own the wire.
        return httpx.Client(transport=self._transport, **kwargs)


# ---------------------------------------------------------------------------
# Scripted outcome builders
# ---------------------------------------------------------------------------


def _voyage_embed_200(dims: int = 1024) -> Callable[[httpx.Request], httpx.Response]:
    """Build a Voyage embeddings 200 response matching the count of input texts."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        texts = payload["input"]
        data = [{"embedding": [0.1] * dims} for _ in texts]
        return httpx.Response(200, json={"data": data}, request=request)

    return _outcome


def _http_429() -> Callable[[httpx.Request], httpx.Response]:
    """Build a genuine HTTP 429 (Retry-After: 0 -> instant retry in tests)."""

    def _outcome(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"retry-after": "0"},
            json={"error": "rate limited"},
            request=request,
        )

    return _outcome


# ---------------------------------------------------------------------------
# Provider builder
# ---------------------------------------------------------------------------


def _voyage_provider(factory: _ScriptedClientFactory) -> Any:
    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    return VoyageAIClient(VoyageAIConfig(), http_client_factory=factory)


# ---------------------------------------------------------------------------
# Fake clock — injectable into AimdController._time_fn
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock for AIMD cooldown (no sleeping)."""

    def __init__(self) -> None:
        self._t = 1000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


# ---------------------------------------------------------------------------
# Governor builder
# ---------------------------------------------------------------------------


def _build_governor(
    *, max_concurrency: int = K_MIN, clock: Optional[_FakeClock] = None
) -> ProviderConcurrencyGovernor:
    """Build a governor with a deterministic injected AIMD clock on every lane.

    A fixed ``_FakeClock`` is ALWAYS injected (a fresh one when the caller does
    not supply its own), removing any dependency on wall-clock ``time.monotonic``
    in the AIMD cooldown path — the harness is fully deterministic.  A test that
    needs to exercise cooldown grow-back can pass its own clock and ``advance()``
    it; the tests here only drive K downward (429 -> floor), which is
    cooldown-independent, so the fixed clock never needs advancing.
    """
    gov = ProviderConcurrencyGovernor(max_concurrency=max_concurrency)
    injected = clock if clock is not None else _FakeClock()
    for lane in (VOYAGE_EMBED, VOYAGE_RERANK, COHERE_EMBED, COHERE_RERANK):
        gov.aimd(lane)._time_fn = injected  # type: ignore[attr-defined]
    return gov


# ---------------------------------------------------------------------------
# Lane-saturation + coalesced-burst helpers (from 1079 harness)
# ---------------------------------------------------------------------------


def _saturate_lane(
    gov: ProviderConcurrencyGovernor, lane: str, *, holders: int
) -> Tuple[threading.Event, List[threading.Thread]]:
    """Pin ``holders`` governor slots on ``lane`` via a gated wire (no sleep).

    Each holder occupies one slot until the returned Event is set.  Returns
    (gate, holder_threads).  The caller MUST set the gate and join the holders.

    On saturation failure, releases the gate and joins holders before raising so
    threads can never leak.
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

    # Wait (bounded) until the lane is actually saturated.
    witness = threading.Event()
    waited = 0.0
    limiter = gov._limiters[lane]  # type: ignore[attr-defined]
    while limiter.in_flight < holders and waited < SATURATION_TIMEOUT_SECONDS:
        witness.wait(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
    if limiter.in_flight != holders:
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

    Saturates the lane so the coalescer dispatcher parks waiting for a slot;
    submits all texts and confirms (via open_batch inspection) that every one
    enqueued into ONE batch; THEN releases the holders so the dispatcher seals
    and dispatches the full batch.

    The gate release + holder join run in finally so a failed witness/submit
    can never leak holder threads.  Collects (vector, exc) per caller.
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
        except BaseException as exc:  # noqa: BLE001
            results[idx] = (None, exc)

    def _open_batch_size() -> int:
        with coalescer._lock:  # type: ignore[attr-defined]
            ob = coalescer._open_batch  # type: ignore[attr-defined]
            return len(ob) if ob is not None else 0

    try:
        # STAGGERED, CONFIRMED enqueue — launch caller i, wait until the open
        # batch reflects i+1 entries before launching i+1.  The dispatcher
        # (caller 0) parks on the saturated lane keeping the batch open.
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
                if max_seen >= target_size:
                    break
                if idx == n - 1 and max_seen >= n - 1 and size == 0:
                    # Final caller's enqueue triggered the seal -> batch cleared.
                    max_seen = n
                    break
                witness.wait(POLL_INTERVAL_SECONDS)
                waited += POLL_INTERVAL_SECONDS
            assert max_seen >= target_size, (
                f"caller {idx} did not enqueue into the open batch: "
                f"peak size={max_seen} (expected >= {target_size})"
            )

        assert max_seen >= n, (
            f"coalescing did not accumulate all callers into one batch: "
            f"peak open_batch size={max_seen} (expected >= {n})"
        )
    finally:
        gate.set()
        for t in holder_threads:
            t.join(timeout=HOLDER_JOIN_TIMEOUT_SECONDS)

    for t in threads:
        t.join(timeout=CALLER_JOIN_TIMEOUT_SECONDS)
    assert not any(t.is_alive() for t in threads), "a coalesced caller hung"
    assert not any(t.is_alive() for t in holder_threads), "a holder hung"
    return results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dummy_keys() -> Any:
    """Provide dummy provider API keys (constructors check presence only)."""
    prev = {k: os.environ.get(k) for k in ("VOYAGE_API_KEY", "CO_API_KEY")}
    os.environ["VOYAGE_API_KEY"] = "dummy-voyage-key-1128"
    os.environ["CO_API_KEY"] = "dummy-cohere-key-1128"
    yield
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _reset_singletons() -> Any:
    """Isolate process-global singletons between tests.

    Both the governor AND ProviderHealthMonitor must be reset: scripted 429s
    can sinbin a lane and leak that state into the next test.
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

    The retry loop still runs (classification, attempt counting, slot
    re-acquisition) — only the wall-clock sleep is elided.
    """
    monkeypatch.setattr(
        "code_indexer.services.provider_backoff._compute_sleep",
        lambda exc, cap: 0.0,
    )


# ===========================================================================
# AC1 — N concurrent submits => ONE coalesced provider call
# ===========================================================================


def test_ac1_n_concurrent_submits_coalesce_into_one_call() -> None:
    """N concurrent submits to the :embed lane produce exactly ONE HTTP call.

    Uses the deterministic _coalesced_burst helper: saturates the lane with
    gated holders so the dispatcher parks; accumulates N callers into one open
    batch; releases holders; asserts ONE provider HTTP call and all vectors.

    This property has NO REST/MCP exposure — the transport call_count is the
    only way to observe it, hence the in-process harness.
    """
    # Seed K at floor (8) — lane saturation needs exactly K holders.
    gov = _build_governor(max_concurrency=K_MIN)

    transport = _ScriptedTransport([_voyage_embed_200()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))
    coalescer = EmbeddingCoalescer(
        VOYAGE_EMBED,
        provider,
        governor=gov,
        acquire_timeout=5.0,
        coalesce_max_batch_size=96,
    )

    n = 5
    texts = [f"ac1 text {i}" for i in range(n)]
    results = _coalesced_burst(coalescer, texts, gov=gov)

    # Every caller receives a vector — no failures.
    for idx, (vec, exc) in enumerate(results):
        assert exc is None, f"caller {idx} failed unexpectedly: {exc!r}"
        assert isinstance(vec, list) and len(vec) == 1024, (
            f"caller {idx} did not get a 1024-dim vector: {vec!r}"
        )

    # EXACTLY ONE provider HTTP call for the entire batch (the core AC1 assertion).
    assert transport.call_count == 1, (
        f"AC1 FAILED: expected exactly 1 provider HTTP call for the coalesced "
        f"batch; got {transport.call_count}.  The batch sub-split or was not "
        "coalesced."
    )

    # The single request carried ALL n texts.
    payload = json.loads(transport.requests[0].content.decode())
    assert len(payload["input"]) == n, (
        f"AC1: the single HTTP call carried {len(payload['input'])} texts "
        f"(expected {n})"
    )


# ===========================================================================
# AC2 — 429 halves the faulted lane's K; other lane unchanged
# ===========================================================================


def test_ac2_429_halves_faulted_lane_other_lane_unchanged() -> None:
    """A 429 on voyage:embed halves that lane's K; cohere:embed K is unchanged.

    Drives the REAL Voyage embed query path through execute_with_backoff +
    governor.  The 429 surfaces as an intact httpx 429 -> governor records a
    multiplicative decrease.  governor.current_k is read directly — NO REST/MCP
    equivalent exists (that is exactly why this harness is in-process).
    """
    # Seed K above the floor so the halving is clearly observable (16 -> 8).
    gov = _build_governor(max_concurrency=16)
    before_k = dict(gov.current_k)

    assert before_k[VOYAGE_EMBED] == 16, (
        f"precondition: voyage:embed K should be 16, got {before_k[VOYAGE_EMBED]}"
    )
    assert before_k[COHERE_EMBED] == 16, (
        f"precondition: cohere:embed K should be 16, got {before_k[COHERE_EMBED]}"
    )

    # Script the transport to ALWAYS 429 — execute_with_backoff will exhaust
    # all 3 attempts (1 initial + 2 retries), each recording a 429 on the
    # governor.  After 3 multiplicative halvings: 16 -> 8 -> 8 (floor) -> 8.
    transport = _ScriptedTransport([_http_429()])
    provider = _voyage_provider(_ScriptedClientFactory(transport))

    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: gov.execute(
                VOYAGE_EMBED,
                lambda: provider.get_embedding("ac2 429 probe"),
                acquire_timeout=5.0,
            )
        )

    after_k = dict(gov.current_k)

    # Faulted lane: K must have HALVED (floor = K_MIN = 8).
    assert after_k[VOYAGE_EMBED] == K_MIN, (
        f"AC2 FAILED: voyage:embed K should have halved to {K_MIN}; "
        f"got {after_k[VOYAGE_EMBED]} (was {before_k[VOYAGE_EMBED]})"
    )

    # Other lanes: COMPLETELY UNCHANGED — per-lane isolation invariant.
    assert after_k[COHERE_EMBED] == before_k[COHERE_EMBED], (
        f"AC2 FAILED (per-lane isolation broken): cohere:embed K changed from "
        f"{before_k[COHERE_EMBED]} to {after_k[COHERE_EMBED]} due to a "
        "voyage:embed 429 — lanes are NOT independent"
    )
    assert after_k[VOYAGE_RERANK] == before_k[VOYAGE_RERANK], (
        f"AC2 FAILED: voyage:rerank K changed from {before_k[VOYAGE_RERANK]} "
        f"to {after_k[VOYAGE_RERANK]} due to a voyage:embed 429"
    )
    assert after_k[COHERE_RERANK] == before_k[COHERE_RERANK], (
        f"AC2 FAILED: cohere:rerank K changed from {before_k[COHERE_RERANK]} "
        f"to {after_k[COHERE_RERANK]} due to a voyage:embed 429"
    )


# ===========================================================================
# Mutation — shared-lock injection detects cross-lane K bleed
# ===========================================================================


class _SharedLockGovernor(ProviderConcurrencyGovernor):
    """MUTATION variant: collapse the two ``:embed`` lanes onto ONE shared
    ``AimdController`` + ONE shared ``ResizableLimiter``, removing per-lane K
    isolation.

    In the REAL governor every lane owns a distinct ``AimdController`` and
    ``current_k`` reads ``aimd.k`` per lane, so a 429 that halves
    ``voyage:embed``'s controller can never change ``cohere:embed``'s K.

    Here both embed lanes are deliberately wired to the SAME ``AimdController``
    instance (backed by one shared ``ResizableLimiter``).  A 429 on
    ``voyage:embed`` halves that shared controller's K, and because
    ``cohere:embed`` reads the very same object, the drop is observed on both
    lanes — the cross-lane bleed the per-lane design is meant to prevent.

    This is the faithful "remove the per-lane domain" mutation: isolation lives
    in the per-lane ``AimdController`` (the K-bearing state), not merely in a
    per-lane mutex, so the mutation shares the state itself.
    """

    def __init__(self, max_concurrency: int = 16) -> None:
        super().__init__(max_concurrency=max_concurrency)
        # ONE limiter + ONE AIMD controller shared across BOTH embed lanes.
        shared_limiter = ResizableLimiter(
            initial=max_concurrency, k_min=K_MIN, k_max=K_MAX
        )
        shared_aimd = AimdController(limiter=shared_limiter, k_min=K_MIN, k_max=K_MAX)
        for lane in (VOYAGE_EMBED, COHERE_EMBED):
            self._limiters[lane] = shared_limiter
            self._aimd[lane] = shared_aimd


def test_mutation_shared_lock_detects_cross_lane_k_bleed() -> None:
    """MUTATION: a shared-lock governor shows cross-lane K bleed; the real one does not.

    1. With the REAL governor: a 429 on voyage:embed leaves cohere:embed's K
       unchanged (per-lane isolation holds).
    2. With the SHARED-LOCK governor (_SharedLockGovernor): a 429 on voyage:embed
       ALSO drives down cohere:embed's K (bleed detected).

    This proves that the per-lane Condition lock domain in the real governor is
    the mechanism that enforces isolation.  If a future refactor collapses the
    lock domains, this test will fail on part 1 (the REAL governor starts
    bleeding) — catching the regression.
    """
    # --- Part 1: REAL governor — no bleed ---
    real_gov = _build_governor(max_concurrency=16)
    before_real = dict(real_gov.current_k)

    transport_real = _ScriptedTransport([_http_429()])
    provider_real = _voyage_provider(_ScriptedClientFactory(transport_real))

    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: real_gov.execute(
                VOYAGE_EMBED,
                lambda: provider_real.get_embedding("mutation real probe"),
                acquire_timeout=5.0,
            )
        )

    after_real = dict(real_gov.current_k)

    # voyage:embed decreased.
    assert after_real[VOYAGE_EMBED] == K_MIN, (
        f"REAL governor: voyage:embed should have halved to {K_MIN}; "
        f"got {after_real[VOYAGE_EMBED]}"
    )
    # cohere:embed UNCHANGED — no bleed.
    assert after_real[COHERE_EMBED] == before_real[COHERE_EMBED], (
        f"REAL governor UNEXPECTEDLY bleeding: cohere:embed changed from "
        f"{before_real[COHERE_EMBED]} to {after_real[COHERE_EMBED]} due to a "
        "voyage:embed 429 — per-lane isolation is BROKEN in the real governor"
    )

    # --- Part 2: SHARED-LOCK governor — bleed EXPECTED ---
    # Reset singletons between the two sub-parts.
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()

    shared_gov = _SharedLockGovernor(max_concurrency=16)
    before_shared = dict(shared_gov.current_k)

    transport_shared = _ScriptedTransport([_http_429()])
    provider_shared = _voyage_provider(_ScriptedClientFactory(transport_shared))

    with pytest.raises(ProviderRateLimitedError):
        execute_with_backoff(
            lambda: shared_gov.execute(
                VOYAGE_EMBED,
                lambda: provider_shared.get_embedding("mutation shared probe"),
                acquire_timeout=5.0,
            )
        )

    after_shared = dict(shared_gov.current_k)

    # voyage:embed halved by its (now shared) AimdController.
    assert after_shared[VOYAGE_EMBED] == K_MIN, (
        f"SHARED governor: voyage:embed should have halved to {K_MIN}; "
        f"got {after_shared[VOYAGE_EMBED]}"
    )
    # cohere:embed ALSO reads K_MIN — the cross-lane BLEED.  Both embed lanes
    # share ONE AimdController, so halving voyage:embed's K is observed here too.
    assert after_shared[COHERE_EMBED] == K_MIN, (
        f"SHARED-LOCK governor: expected cross-lane K bleed — cohere:embed "
        f"should read {K_MIN} (shared AimdController), got "
        f"{after_shared[COHERE_EMBED]}"
    )
    assert after_shared[COHERE_EMBED] != before_shared[COHERE_EMBED], (
        f"SHARED-LOCK governor: cohere:embed K did not change "
        f"({before_shared[COHERE_EMBED]} -> {after_shared[COHERE_EMBED]}); "
        "the mutation failed to demonstrate cross-lane bleed"
    )
