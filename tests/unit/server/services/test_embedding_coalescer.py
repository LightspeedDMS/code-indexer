"""Unit tests for EmbeddingCoalescer (Story #1079 Phase D).

The coalescer accretes single-text embedding requests into ONE sealed batch
dispatched through the EXISTING ProviderConcurrencyGovernor as the SOLE limiter
(no separate semaphore / in_flight counter). The governor slot-wait IS the
accumulation window: while the dispatcher is parked waiting for a slot, late
arrivals join the open batch; the first attempt that gets a slot seals it.

Tests use a deterministic scripted fake provider (counts get_embeddings_batch
HTTP calls, controllable latency, scriptable exceptions, exposes the SAME
adapter methods the real providers use) and the REAL ProviderConcurrencyGovernor
(Phase C) with a small K to force saturation/coalescing (anti-mock: exercise the
real limiter). The governor clamps initial K into [K_MIN=8, K_MAX=32], so
saturation tests spawn 8 blocker threads + N submitters.
"""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import httpx
import pytest

from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer
from code_indexer.server.services.provider_concurrency_governor import (
    GovernorBusyError,
    ProviderConcurrencyGovernor,
)

LANE = "voyage:embed"
GOV_K = 8  # K_MIN — smallest the limiter will clamp to


def _make_429_exc() -> httpx.HTTPStatusError:
    """A canonical 429 that provider_backoff.is_rate_limited() recognizes."""
    request = httpx.Request("POST", "https://api.example.com/embed")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _text_id(text: str) -> int:
    """Map 'text-<n>' -> n for deterministic vector identity."""
    return int(text.rsplit("-", 1)[-1])


@pytest.fixture(autouse=True)
def _reset_singletons():
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()


# ---------------------------------------------------------------------------
# Deterministic scripted fake providers
# ---------------------------------------------------------------------------


class FakeVoyageProvider:
    """Voyage-shaped fake: token-only split, NO _get_texts_per_request.

    Each text counts as ``tokens_per_text`` tokens. get_embeddings_batch returns
    a distinct deterministic vector per text ([id, 0.0]) so demux/order can be
    asserted; counts calls and records per-call batch sizes; supports per-call
    latency, a scripted exception sequence, and a wrong return-count override.
    """

    def __init__(
        self,
        token_limit: int = 120000,
        tokens_per_text: int = 1,
        latency: float = 0.0,
        exceptions: Optional[List[Optional[BaseException]]] = None,
        return_count_override: Optional[int] = None,
    ) -> None:
        self._token_limit = token_limit
        self._tokens_per_text = tokens_per_text
        self._latency = latency
        self._exceptions = list(exceptions) if exceptions else []
        self._return_count_override = return_count_override
        self.call_count = 0
        self.batch_sizes: List[int] = []
        self._lock = threading.Lock()

    def _count_tokens_accurately(self, text: str) -> int:
        return self._tokens_per_text

    def _get_model_token_limit(self) -> int:
        return self._token_limit

    def get_embeddings_batch(
        self, texts: List[str], *, retry: bool = True
    ) -> List[List[float]]:
        with self._lock:
            idx = self.call_count
            self.call_count += 1
            self.batch_sizes.append(len(texts))
        if self._latency:
            time.sleep(self._latency)
        scripted = self._exceptions[idx] if idx < len(self._exceptions) else None
        if scripted is not None:
            raise scripted
        n = (
            self._return_count_override
            if self._return_count_override is not None
            else len(texts)
        )
        return [[float(_text_id(t)), 0.0] for t in texts[:n]]


class FakeCohereProvider(FakeVoyageProvider):
    """Cohere-shaped fake: dual-constraint split WITH _get_texts_per_request."""

    def __init__(self, texts_per_request: int = 96, **kwargs) -> None:
        super().__init__(**kwargs)
        self._texts_per_request = texts_per_request

    def _count_tokens(self, text: str) -> int:
        return self._tokens_per_text

    def _get_texts_per_request(self) -> int:
        return self._texts_per_request

    # Voyage-only counter absent so the resolver picks _count_tokens.
    _count_tokens_accurately = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Saturation harness: hold all K slots, run N coalesced submits, return outcome
# ---------------------------------------------------------------------------


class _Outcome:
    def __init__(self) -> None:
        self.results: Dict[int, List[float]] = {}
        self.errors: Dict[int, BaseException] = {}


def _saturate(governor: ProviderConcurrencyGovernor, lane: str, hold: threading.Event):
    """Occupy all GOV_K slots of ``lane`` until ``hold`` is set."""
    bar = threading.Barrier(GOV_K + 1)
    threads: List[threading.Thread] = []

    def _blocker():
        def _h():
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
    n: int,
    *,
    release_before_join: bool = True,
    accumulate: float = 0.3,
) -> _Outcome:
    """Submit ``n`` texts ('text-0'..'text-{n-1}') concurrently against a fully
    saturated lane so they coalesce, then release the lane and collect results.

    When ``release_before_join`` is False the slots are released only AFTER all
    submitters have returned (used for the never-gets-a-slot GovernorBusy path).
    """
    hold = threading.Event()
    blockers = _saturate(governor, lane, hold)
    outcome = _Outcome()
    start = threading.Barrier(n)

    def _submit(i: int):
        start.wait()
        try:
            outcome.results[i] = coalescer.submit(f"text-{i}")
        except BaseException as ex:  # noqa: BLE001
            outcome.errors[i] = ex

    with ThreadPoolExecutor(max_workers=n) as ex:
        futs = [ex.submit(_submit, i) for i in range(n)]
        if release_before_join:
            time.sleep(accumulate)  # let all N accrete into open batches
            hold.set()
            for f in futs:
                f.result(timeout=30)
        else:
            for f in futs:
                f.result(timeout=30)
            hold.set()
    for t in blockers:
        t.join(timeout=5)
    return outcome


# ===========================================================================
# Resolver: texts_cap / token_limit / token_counter per provider
# ===========================================================================


class TestResolver:
    def test_voyage_token_limit_is_90_percent_of_spec(self):
        c = EmbeddingCoalescer(
            LANE,
            FakeVoyageProvider(token_limit=120000),
            governor=ProviderConcurrencyGovernor(GOV_K),
        )
        assert c.token_limit == int(120000 * 0.9)  # 108000, READ FROM SPEC

    def test_voyage_token_limit_reads_from_spec_not_hardcoded(self):
        c = EmbeddingCoalescer(
            LANE,
            FakeVoyageProvider(token_limit=320000),
            governor=ProviderConcurrencyGovernor(GOV_K),
        )
        assert c.token_limit == int(320000 * 0.9)  # 288000
        assert c.token_limit != 108000

    def test_voyage_texts_cap_falls_back_to_ceiling(self):
        c = EmbeddingCoalescer(
            LANE,
            FakeVoyageProvider(),
            governor=ProviderConcurrencyGovernor(GOV_K),
            coalesce_max_batch_size=50,
        )
        assert c.texts_cap == 50  # Voyage has no _get_texts_per_request

    def test_cohere_texts_cap_is_min_of_ceiling_and_provider(self):
        c = EmbeddingCoalescer(
            "cohere:embed",
            FakeCohereProvider(texts_per_request=96),
            governor=ProviderConcurrencyGovernor(GOV_K),
            coalesce_max_batch_size=200,
        )
        assert c.texts_cap == 96  # min(200, 96)

    def test_cohere_texts_cap_ceiling_wins_when_smaller(self):
        c = EmbeddingCoalescer(
            "cohere:embed",
            FakeCohereProvider(texts_per_request=96),
            governor=ProviderConcurrencyGovernor(GOV_K),
            coalesce_max_batch_size=10,
        )
        assert c.texts_cap == 10  # min(10, 96)

    def test_token_counter_uses_voyage_adapter(self):
        c = EmbeddingCoalescer(
            LANE,
            FakeVoyageProvider(tokens_per_text=7),
            governor=ProviderConcurrencyGovernor(GOV_K),
        )
        assert c.count_tokens("anything") == 7

    def test_token_counter_uses_cohere_adapter(self):
        c = EmbeddingCoalescer(
            "cohere:embed",
            FakeCohereProvider(tokens_per_text=3),
            governor=ProviderConcurrencyGovernor(GOV_K),
        )
        assert c.count_tokens("anything") == 3

    def test_provider_without_token_counter_raises(self):
        """A provider exposing neither adapter counter is a hard construction
        error (no silent fallback — Messi #2)."""

        class NoCounter:
            def _get_model_token_limit(self) -> int:
                return 120000

        with pytest.raises(AttributeError):
            EmbeddingCoalescer(
                LANE, NoCounter(), governor=ProviderConcurrencyGovernor(GOV_K)
            )


# ===========================================================================
# Single-text happy path
# ===========================================================================


class TestSingleSubmit:
    def test_single_submit_returns_vector(self):
        prov = FakeVoyageProvider()
        c = EmbeddingCoalescer(LANE, prov, governor=ProviderConcurrencyGovernor(GOV_K))
        assert c.submit("text-5") == [5.0, 0.0]
        assert prov.call_count == 1
        assert prov.batch_sizes == [1]


# ===========================================================================
# Dispatcher election + order-preserving demux under saturation
# ===========================================================================


class TestDispatcherElectionAndDemux:
    def test_exactly_one_dispatcher_order_preserved(self):
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.05)
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=30.0,
        )
        n = 6
        out = _run_saturated_submits(c, governor, LANE, n)
        assert not out.errors, f"unexpected errors: {out.errors}"
        # ONE coalesced HTTP call for all N submits, batch size N.
        assert prov.call_count == 1
        assert prov.batch_sizes == [n]
        # Order-preserving demux: each caller got its own correct vector.
        for i in range(n):
            assert out.results[i] == [float(i), 0.0]


# ===========================================================================
# Seal-once across execute_with_backoff retry (scripted 429 then success)
# ===========================================================================


class TestSealOnceAcrossRetry:
    def test_single_429_then_success_one_logical_batch(self):
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.05, exceptions=[_make_429_exc(), None])
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=30.0,
        )
        n = 4
        out = _run_saturated_submits(c, governor, LANE, n)
        assert not out.errors, f"unexpected errors: {out.errors}"
        # Two HTTP attempts (429 then success) but IDENTICAL membership both times.
        assert prov.call_count == 2
        assert prov.batch_sizes == [n, n]
        for i in range(n):
            assert out.results[i] == [float(i), 0.0]


# ===========================================================================
# Dual-constraint early sealing under saturation (proves real batch splits)
# ===========================================================================


class TestDualConstraintSealing:
    def test_texts_cap_seals_then_new_batch(self):
        """texts_cap == 3, six coalesced submits -> batches [3, 3]: the open
        batch seals at the cap and the next arrival starts a new batch."""
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.05)
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=3,
            acquire_timeout=30.0,
        )
        out = _run_saturated_submits(c, governor, LANE, 6)
        assert not out.errors, out.errors
        # Two sealed batches of exactly the cap — NO sub-split, NO over-fill.
        assert sorted(prov.batch_sizes) == [3, 3], prov.batch_sizes
        assert prov.call_count == 2
        for i in range(6):
            assert out.results[i] == [float(i), 0.0]

    def test_token_limit_seals_then_new_batch(self):
        """token_limit == 900 (spec 1000 * 0.9), tokens_per_text == 400. Adding a
        3rd text (800+400=1200 > 900) would exceed -> seal. Six submits -> three
        batches of 2 each. Threshold matches the provider split predicate."""
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(token_limit=1000, tokens_per_text=400, latency=0.05)
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=30.0,
        )
        assert c.token_limit == 900  # int(1000 * 0.9), READ FROM SPEC
        out = _run_saturated_submits(c, governor, LANE, 6)
        assert not out.errors, out.errors
        # Each batch holds exactly 2 (800 tokens <= 900; a 3rd would exceed).
        assert sorted(prov.batch_sizes) == [2, 2, 2], prov.batch_sizes
        for i in range(6):
            assert out.results[i] == [float(i), 0.0]


# ===========================================================================
# texts_cap == 1 immediate-seal edge
# ===========================================================================


class TestTextsCapOne:
    def test_cap_one_each_gets_own_batch(self):
        """texts_cap == 1: the first dispatcher seals immediately, so a late
        joiner can't exceed the cap and opens its own new batch."""
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.05)
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=1,
            acquire_timeout=30.0,
        )
        out = _run_saturated_submits(c, governor, LANE, 4)
        assert not out.errors, out.errors
        assert prov.batch_sizes == [1, 1, 1, 1]
        assert prov.call_count == 4
        for i in range(4):
            assert out.results[i] == [float(i), 0.0]


# ===========================================================================
# Count-mismatch -> ValueError, shared-fate, survives python -O
# ===========================================================================


class TestCountMismatch:
    def test_count_mismatch_raises_value_error_shared_fate(self):
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.05, return_count_override=1)
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=30.0,
        )
        out = _run_saturated_submits(c, governor, LANE, 4)
        # Shared-fate: EVERY caller receives the ValueError.
        assert len(out.errors) == 4 and not out.results
        for i in range(4):
            assert isinstance(out.errors[i], ValueError), out.errors[i]

    def test_count_mismatch_survives_python_O(self):
        """ValueError (not assert) must fire even under python -O / PYTHONOPTIMIZE."""
        code = (
            "import threading, time\n"
            "from concurrent.futures import ThreadPoolExecutor\n"
            "from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer\n"
            "from code_indexer.server.services.provider_concurrency_governor import ProviderConcurrencyGovernor\n"
            "ProviderConcurrencyGovernor.reset_instance()\n"
            "class P:\n"
            "    def __init__(self): self.call_count=0\n"
            "    def _count_tokens_accurately(self,t): return 1\n"
            "    def _get_model_token_limit(self): return 120000\n"
            "    def get_embeddings_batch(self,texts,*,retry=True):\n"
            "        self.call_count+=1; time.sleep(0.05); return [[0.0,0.0]]\n"
            "gov=ProviderConcurrencyGovernor(8)\n"
            "c=EmbeddingCoalescer('voyage:embed',P(),governor=gov,coalesce_max_batch_size=96,acquire_timeout=30.0)\n"
            "hold=threading.Event(); bar=threading.Barrier(9)\n"
            "def blk():\n"
            "    def h(): bar.wait(); hold.wait(10); return 'ok'\n"
            "    gov.execute('voyage:embed',h,acquire_timeout=30.0)\n"
            "ts=[threading.Thread(target=blk,daemon=True) for _ in range(8)]\n"
            "[t.start() for t in ts]; bar.wait()\n"
            "errs={}; sb=threading.Barrier(3)\n"
            "def sub(i):\n"
            "    sb.wait()\n"
            "    try: c.submit('text-%d'%i); errs[i]='NOERR'\n"
            "    except ValueError: errs[i]='VE'\n"
            "    except Exception as e: errs[i]=type(e).__name__\n"
            "with ThreadPoolExecutor(max_workers=3) as ex:\n"
            "    fs=[ex.submit(sub,i) for i in range(3)]\n"
            "    time.sleep(0.3); hold.set(); [f.result(timeout=30) for f in fs]\n"
            "assert all(v=='VE' for v in errs.values()), errs\n"
            "print('OPT_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-O", "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PYTHONPATH": "./src", "PYTHONOPTIMIZE": "1", "PATH": "/usr/bin:/bin"},
        )
        assert "OPT_OK" in result.stdout, (
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )


# ===========================================================================
# Shared-fate: GovernorBusyError when NO slot is ever granted
# ===========================================================================


class TestSharedFateNoSlot:
    def test_governor_busy_fans_out_to_all_callers(self):
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider()
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=0.2,
        )
        out = _run_saturated_submits(c, governor, LANE, 4, release_before_join=False)
        # Every caller receives GovernorBusyError; none hangs; provider untouched.
        assert len(out.errors) == 4 and not out.results
        for i in range(4):
            assert isinstance(out.errors[i], GovernorBusyError), out.errors[i]
        assert prov.call_count == 0


# ===========================================================================
# Backoff happens with NO slot held (slot released between attempts)
# ===========================================================================


class TestBackoffReleasesSlot:
    def test_slot_released_during_backoff_sleep(self):
        governor = ProviderConcurrencyGovernor(GOV_K)
        prov = FakeVoyageProvider(latency=0.1, exceptions=[_make_429_exc(), None])
        c = EmbeddingCoalescer(
            LANE,
            prov,
            governor=governor,
            coalesce_max_batch_size=96,
            acquire_timeout=30.0,
        )
        limiter = governor._limiters[LANE]  # white-box: live in_flight telemetry
        samples: List[int] = []
        stop = threading.Event()

        def _sampler():
            while not stop.is_set():
                samples.append(limiter.in_flight)
                time.sleep(0.005)

        sampler = threading.Thread(target=_sampler, daemon=True)
        sampler.start()
        vec = c.submit("text-9")
        stop.set()
        sampler.join(timeout=5)

        assert vec == [9.0, 0.0]
        assert prov.call_count == 2  # 429 then success
        assert 1 in samples, "slot was never observed held"
        assert 0 in samples, "slot was never observed released during backoff"
