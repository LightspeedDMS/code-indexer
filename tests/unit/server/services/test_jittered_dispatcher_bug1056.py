"""
Unit tests for jittered_dispatcher module (Bug #1056).

RED phase: written before the module exists to drive the implementation.
"""

import time
from concurrent.futures import as_completed
from typing import List


# ---------------------------------------------------------------------------
# Tests for dispatch_parallel_with_jitter
# ---------------------------------------------------------------------------


def test_dispatch_parallel_with_jitter_smooths_concurrent_submissions() -> None:
    """Submit 10 items with concurrency=4 and base_jitter=0.1s.

    Each worker records its entry time via time.monotonic().
    The spread between first and last worker entry must be >= 0.05s,
    proving that jitter is paid by workers before doing work.
    """
    from code_indexer.server.services.jittered_dispatcher import (
        dispatch_parallel_with_jitter,
    )

    entry_times: List[float] = []

    def recording_worker(item: int) -> int:
        entry_times.append(time.monotonic())
        return item

    futures = dispatch_parallel_with_jitter(
        list(range(10)),
        concurrency=4,
        base_jitter_seconds=0.1,
        worker_fn=recording_worker,
    )
    # Drain all futures to ensure all workers have completed.
    for _ in as_completed(futures):
        pass

    assert len(entry_times) == 10, f"Expected 10 entry times, got {len(entry_times)}"
    spread = max(entry_times) - min(entry_times)
    assert spread >= 0.05, (
        f"Expected spread >= 0.05s (jitter smoothing), got {spread:.4f}s. "
        "Workers likely all started in lockstep."
    )


def test_dispatch_parallel_with_jitter_zero_jitter_disables_jitter() -> None:
    """base_jitter=0 must disable jitter — time.sleep must never be invoked.

    Bug #1381: previously asserted a hard real-wall-clock spread bound
    (`< 0.05s`) across actual ThreadPoolExecutor worker entry timestamps,
    which is inherently sensitive to CPU scheduling contention under
    full-suite/full-chunk concurrent load (observed failure: `got 0.4636s`).
    Replaced with a deterministic assertion on the dispatcher's own jitter
    invariant: `_jittered()`'s `if jitter > 0` guard must never call
    `time.sleep` at all when `base_jitter_seconds<=0`.

    Proven by patching the `time` NAME binding inside the jittered_dispatcher
    module's own namespace (`patch.object(jd_mod, "time", ...)`) rather than
    an attribute on the shared global `time` module singleton (which would
    reintroduce the exact same cross-thread interference fragility class
    fixed for bug #1375/#1381 elsewhere) — no other concurrently-running
    thread in the same pytest process can ever observe or trip this patch,
    since only code that resolves `time` via jittered_dispatcher's own
    module globals (i.e. `_jittered`'s `time.sleep(...)` call) is affected.
    """
    from unittest.mock import MagicMock, patch

    import code_indexer.server.services.jittered_dispatcher as jd_mod

    def recording_worker(item: int) -> int:
        return item

    mock_time = MagicMock()
    with patch.object(jd_mod, "time", mock_time):
        futures = jd_mod.dispatch_parallel_with_jitter(
            list(range(10)),
            concurrency=10,
            base_jitter_seconds=0.0,
            worker_fn=recording_worker,
        )
        for _ in as_completed(futures):
            pass

    mock_time.sleep.assert_not_called()


def test_dispatch_parallel_with_jitter_preserves_input_order_of_futures() -> None:
    """Futures must be returned in the same order as the input items list."""
    from code_indexer.server.services.jittered_dispatcher import (
        dispatch_parallel_with_jitter,
    )

    items = list(range(8))
    results_by_index: List[int] = [0] * len(items)

    def identity_worker(item: int) -> int:
        return item * 2

    futures = dispatch_parallel_with_jitter(
        items,
        concurrency=4,
        base_jitter_seconds=0.0,
        worker_fn=identity_worker,
    )

    assert len(futures) == len(items), "futures list length must match items length"
    for idx, future in enumerate(futures):
        results_by_index[idx] = future.result(timeout=5.0)

    expected = [i * 2 for i in items]
    assert results_by_index == expected, (
        f"Futures not in input order. Got {results_by_index}, expected {expected}"
    )


def test_dispatch_parallel_with_jitter_propagates_worker_exceptions() -> None:
    """Worker raising ValueError for one item: Future.exception() returns ValueError.
    Other items complete normally.
    """
    from code_indexer.server.services.jittered_dispatcher import (
        dispatch_parallel_with_jitter,
    )

    BOOM_ITEM = 5

    def selective_raiser(item: int) -> int:
        if item == BOOM_ITEM:
            raise ValueError(f"deliberate failure for item {item}")
        return item

    items = list(range(8))
    futures = dispatch_parallel_with_jitter(
        items,
        concurrency=4,
        base_jitter_seconds=0.0,
        worker_fn=selective_raiser,
    )

    # All futures must resolve (no hang).
    exceptions = {}
    results = {}
    for idx, future in enumerate(futures):
        exc = future.exception(timeout=5.0)
        if exc is not None:
            exceptions[items[idx]] = exc
        else:
            results[items[idx]] = future.result()

    assert BOOM_ITEM in exceptions, "Expected ValueError for BOOM_ITEM"
    assert isinstance(exceptions[BOOM_ITEM], ValueError)
    for i in items:
        if i != BOOM_ITEM:
            assert i in results, f"Item {i} should have completed normally"
            assert results[i] == i


# ---------------------------------------------------------------------------
# Tests for sleep_with_jitter
# ---------------------------------------------------------------------------


def test_sleep_with_jitter_sleeps_within_bound() -> None:
    """sleep_with_jitter(0.05) must sleep between 0 and 0.10s (generous upper bound)."""
    from code_indexer.server.services.jittered_dispatcher import sleep_with_jitter

    start = time.monotonic()
    sleep_with_jitter(0.05)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.0, "Elapsed time must be non-negative"
    assert elapsed <= 0.10, (
        f"Expected elapsed <= 0.10s for base_jitter=0.05s, got {elapsed:.4f}s"
    )


def test_sleep_with_jitter_zero_is_noop() -> None:
    """sleep_with_jitter(0) must return immediately (elapsed < 0.005s)."""
    from code_indexer.server.services.jittered_dispatcher import sleep_with_jitter

    start = time.monotonic()
    sleep_with_jitter(0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.005, (
        f"Expected near-zero elapsed for base_jitter=0, got {elapsed:.4f}s"
    )
