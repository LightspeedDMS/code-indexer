"""Bug #1547 round-2 hardening FIX 3 (RED): the cold-key single-flight wait
in TemporalFreshnessSignalCache.get_or_compute must be BOUNDED.

Scope, precisely: when a cached entry EXISTS but is stale, a concurrent
caller for the same key still waits on the in-flight recompute exactly as
before -- that path is UNCHANGED by this fix (not exercised here). The
problem is ONLY the COLD-key path: no cached entry exists AT ALL for this
key, and another thread is already inside a blocking compute() for it. On
today's code that wait (`event.wait()`, no timeout) is unbounded -- on a
`hard` NFS mount, os.stat can block in uninterruptible kernel retry
forever during an outage, and this wait lands on the REQUEST DISPATCH
thread, so it can exhaust the request pool.

Written BEFORE the fix: get_or_compute has no bounded-wait / degraded-
fallback behavior for the cold-key case today, so a second caller for a
cold key whose sibling's compute() never returns blocks indefinitely. The
second call itself runs in a background thread here (rather than directly
on the test's main thread) SPECIFICALLY so this test cannot hang the whole
suite even against the unmodified code -- a bounded thread.join() replaces
the unbounded wait as the thing that can time out, and the assertion on
`is_alive()` is what actually fails RED. The stuck first thread is always
released in a finally block, regardless of which assertion fails.
"""

import threading
import time
from typing import Dict, List

import pytest

#: Bound configured on the cache under test -- short so this test stays
#: fast while still being long enough to unambiguously distinguish "waited
#: about the bound" from "returned immediately" or "hung forever".
_BOUND_SECONDS = 0.3

#: Ceiling the second caller's total wait must stay under to prove it did
#: NOT block indefinitely -- generous slack over the bound to absorb
#: scheduling jitter, but far below what an unbounded hang would produce.
#: Also used as the join() timeout, so a genuinely unbounded wait fails
#: this test in bounded wall-clock time instead of hanging the suite.
_MAX_ALLOWED_ELAPSED_SECONDS = _BOUND_SECONDS + 4.0

#: How long to wait for the first (stuck) thread to confirm it has entered
#: compute() before the test proceeds.
_FIRST_COMPUTE_STARTED_TIMEOUT_SECONDS = 5.0

#: Bound on how long the first (stuck) thread's cleanup join() may block.
_THREAD_JOIN_TIMEOUT_SECONDS = 5.0


class TestColdKeySingleFlightWaitIsBounded:
    def test_second_caller_does_not_block_indefinitely_when_first_computes_never_returns(
        self,
    ):
        from code_indexer.server.services.temporal_freshness_cache import (
            TemporalFreshnessSignalCache,
        )

        cache = TemporalFreshnessSignalCache(
            cold_key_wait_timeout_seconds=_BOUND_SECONDS
        )
        first_compute_started = threading.Event()
        # Deliberately never .set() -- simulates a genuinely hung NFS stat
        # (uninterruptible kernel retry): compute() never returns.
        release_first_compute = threading.Event()

        def hanging_compute(generation: int) -> List[str]:
            first_compute_started.set()
            release_first_compute.wait()
            return ["should-never-be-reached"]

        first_thread = threading.Thread(
            target=lambda: cache.get_or_compute("key-a", hanging_compute),
            daemon=True,
        )
        first_thread.start()
        try:
            started = first_compute_started.wait(
                timeout=_FIRST_COMPUTE_STARTED_TIMEOUT_SECONDS
            )
            assert started, "the first thread never entered compute()"

            second_compute_calls: List[int] = []

            def second_compute(generation: int) -> List[str]:
                second_compute_calls.append(generation)
                return ["should-never-happen"]

            second_result_holder: Dict[str, List[str]] = {}

            def _second_call() -> None:
                second_result_holder["value"] = cache.get_or_compute(
                    "key-a", second_compute
                )

            start = time.monotonic()
            second_thread = threading.Thread(target=_second_call, daemon=True)
            second_thread.start()
            second_thread.join(timeout=_MAX_ALLOWED_ELAPSED_SECONDS)
            elapsed = time.monotonic() - start

            assert not second_thread.is_alive(), (
                "Bug #1547 FIX 3: a second caller for a COLD key whose "
                "sibling's compute() never returns must not block "
                f"indefinitely -- still blocked after {elapsed:.3f}s, "
                f"expected to return within "
                f"{_MAX_ALLOWED_ELAPSED_SECONDS:.3f}s"
            )
            assert not second_compute_calls, (
                "the second caller must NOT invoke compute() itself while "
                "another thread's compute() is still stuck for the same "
                "key -- that would just hang too"
            )
            assert second_result_holder.get("value") is not None
        finally:
            # Always release the stuck first thread, regardless of which
            # assertion above failed, so it can finish.
            release_first_compute.set()
            first_thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
