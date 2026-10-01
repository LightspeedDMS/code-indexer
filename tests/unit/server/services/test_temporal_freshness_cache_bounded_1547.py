"""Bug #1547 Finding 1 (RED): _compute_temporal_freshness_signal() ran
synchronously on EVERY live temporal dispatch, doing a directory scan plus
one os.stat per temporal shard against an NFSv3 `hard` mount (where
os.stat blocks in UNINTERRUPTIBLE kernel retry during an outage, it does
not fail) -- PLUS a golden-lineage metadata-store lookup
(_resolve_golden_temporal_context). 20 concurrent queries against a
70-shard repo == ~1400 independent blocking stats, all in caller threads.

Bug #1538 solved this exact hazard for its own HNSW freshness check with
three properties this fix must mirror: (a) never hold a shared lock while
stat'ing, (b) a per-key in-flight guard so at most ONE thread is inside the
blocking stat per key while others proceed with what they have, (c) a
minimum re-check interval so the stat cannot run per-request.

These are direct unit tests of the new TemporalFreshnessSignalCache class
(properties a/b/c). The wiring proof that execute_live_temporal_search
actually routes through this cache is covered by
test_temporal_dedup_degraded_freshness_collapse_1547.py (Finding 2), which
needs the same real-shard + freshness_cache setup anyway.

Written BEFORE the fix -- code_indexer.server.services.temporal_freshness_cache
does not exist yet, so every test in this module must genuinely fail
against the current (unmodified) code with an ImportError.
"""

import threading
import time
from typing import List

import pytest

#: A recheck interval long enough that a second call issued immediately
#: after the first (no sleep) is unambiguously "within the interval".
_LONG_RECHECK_INTERVAL_SECONDS = 10.0

#: A recheck interval short enough that a bounded, deterministic sleep can
#: reliably land AFTER it elapses without slowing the test suite down.
_SHORT_RECHECK_INTERVAL_SECONDS = 0.05

#: Comfortably past _SHORT_RECHECK_INTERVAL_SECONDS.
_SLEEP_PAST_SHORT_INTERVAL_SECONDS = 0.15

#: Bound on how long a thread.join()/Event.wait() may block before the
#: test itself fails rather than hanging forever.
_THREAD_JOIN_TIMEOUT_SECONDS = 5.0

#: Number of concurrent callers used to exercise the single-flight guard.
_CONCURRENT_CALLER_COUNT = 10


class TestRateLimitedRecompute:
    """Direct unit tests of TemporalFreshnessSignalCache -- properties (a)
    and (c): compute() only runs on a genuine cache miss / stale entry."""

    def test_second_call_within_interval_reuses_cached_signal_without_recomputing(
        self,
    ):
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        cache = TemporalFreshnessSignalCache(
            min_recheck_interval_seconds=_LONG_RECHECK_INTERVAL_SECONDS
        )
        calls: List[int] = []

        def compute(generation):
            calls.append(generation)
            return ["real-signal"]

        first_result = cache.get_or_compute("key-a", compute)
        second_result = cache.get_or_compute("key-a", compute)

        assert first_result == second_result == ["real-signal"]
        assert len(calls) == 1, (
            "Bug #1547 Finding 1: a second call for the SAME key within "
            "the recheck interval must reuse the cached signal without "
            f"recomputing -- compute() was called {len(calls)} times"
        )

    def test_call_after_interval_elapses_recomputes(self):
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        cache = TemporalFreshnessSignalCache(
            min_recheck_interval_seconds=_SHORT_RECHECK_INTERVAL_SECONDS
        )
        calls: List[int] = []

        def compute(generation):
            calls.append(generation)
            return [f"signal-{generation}"]

        first_result = cache.get_or_compute("key-a", compute)
        time.sleep(_SLEEP_PAST_SHORT_INTERVAL_SECONDS)
        second_result = cache.get_or_compute("key-a", compute)

        assert len(calls) == 2, (
            "a call after the recheck interval elapses must trigger a "
            f"fresh recompute -- got {len(calls)} compute() calls"
        )
        # Bug #1547 round-2 FIX 1: the generation counter is now
        # PROCESS-WIDE (shared across every cache instance in this test
        # session), so a freshly constructed cache's first generation is
        # no longer guaranteed to be literal 1 -- assert the RELATIVE
        # property this test actually cares about instead: the two
        # recompute passes get DIFFERENT generations, and each result
        # matches the generation compute() actually observed.
        assert calls[0] != calls[1]
        assert first_result == [f"signal-{calls[0]}"]
        assert second_result == [f"signal-{calls[1]}"]
        assert first_result != second_result


class _ConcurrencyProbe:
    """Bookkeeping for TestSingleFlightBoundsConcurrentBlockingCompute --
    extracted so the test method itself stays short."""

    def __init__(self) -> None:
        self.concurrent_current = 0
        self.concurrent_max = 0
        self.wait_outcomes: List[bool] = []
        self.call_results: List[List[str]] = []
        self.lock = threading.Lock()
        self.release_event = threading.Event()
        self.compute_started = threading.Event()

    def compute(self, generation: int) -> List[str]:
        with self.lock:
            self.concurrent_current += 1
            self.concurrent_max = max(self.concurrent_max, self.concurrent_current)
        self.compute_started.set()
        wait_result = self.release_event.wait(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        with self.lock:
            self.wait_outcomes.append(wait_result)
            self.concurrent_current -= 1
        return ["signal"]

    def run_concurrent_callers(self, cache) -> None:
        def _call():
            result = cache.get_or_compute("key-a", self.compute)
            with self.lock:
                self.call_results.append(result)

        threads = [
            threading.Thread(target=_call) for _ in range(_CONCURRENT_CALLER_COUNT)
        ]
        for t in threads:
            t.start()

        started = self.compute_started.wait(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        assert started, "compute() was never entered by any thread within the timeout"
        self.release_event.set()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
            assert not t.is_alive(), (
                "a thread failed to complete within the join timeout -- "
                "possible deadlock in the single-flight guard"
            )


class TestSingleFlightBoundsConcurrentBlockingCompute:
    """Property (b): at most ONE thread is ever inside the blocking
    compute() for a given key at a time."""

    def test_concurrent_callers_for_same_key_never_run_compute_concurrently(self):
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        cache = TemporalFreshnessSignalCache(
            min_recheck_interval_seconds=_LONG_RECHECK_INTERVAL_SECONDS
        )
        probe = _ConcurrencyProbe()
        probe.run_concurrent_callers(cache)

        assert probe.wait_outcomes, "compute() must have run at least once"
        assert all(probe.wait_outcomes), (
            "release_event.wait() timed out inside compute() for at least "
            "one thread -- the single-flight guard may be deadlocked"
        )
        assert len(probe.call_results) == _CONCURRENT_CALLER_COUNT
        assert all(r == ["signal"] for r in probe.call_results)
        assert probe.concurrent_max == 1, (
            "Bug #1547 Finding 1: at most ONE thread may be inside the "
            "blocking compute() for a given key at a time -- observed "
            f"{probe.concurrent_max} concurrent invocations"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
