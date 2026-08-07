"""Concurrency/linearizability tests for PostgresAliasLockStore (Issue
#1546 Phase 1).

Fixtures (``store``, ``unique_key``, the session-scoped real-migration
bootstrap) live in ``conftest.py``. Basics/ownership-loss/crash-recovery
tests live in ``test_alias_lock_store_postgres_1546.py``.
"""

from __future__ import annotations

import threading
import time
from typing import List, Tuple

from .conftest import postgres_skip_marker

pytestmark = postgres_skip_marker

_THREAD_JOIN_TIMEOUT_SECONDS = 60
_LINEARIZABILITY_NUM_THREADS = 6
_LINEARIZABILITY_ACQUISITIONS_PER_THREAD = 15
_LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER = 50
_HOLD_TIME_SCALE_SECONDS = 0.001
_HOLD_TIME_MODULUS = 3
_GENEROUS_ACQUIRE_TIMEOUT_SECONDS = 5.0
_PROMPT_CONTENTION_DEADLINE_SECONDS = 3.0


def _run_linearizability_worker(
    store,
    unique_key: str,
    operation: str,
    thread_id: int,
    acquisitions_per_thread: int,
    max_attempts_per_thread: int,
    events: List[Tuple[float, float, int]],
    events_lock: threading.Lock,
) -> None:
    """One thread's full acquire/hold/release loop, repeated until it has
    completed `acquisitions_per_thread` successful cycles -- extracted so
    the calling test stays under the project's per-method line budget."""
    completed = 0
    attempts = 0
    while completed < acquisitions_per_thread:
        attempts += 1
        if attempts > max_attempts_per_thread:
            raise AssertionError(
                f"thread {thread_id} exceeded bounded attempt budget "
                f"({max_attempts_per_thread})"
            )
        handle = store.try_acquire(unique_key, operation=operation)
        if handle is None:
            continue
        try:
            start = time.monotonic()
            time.sleep(_HOLD_TIME_SCALE_SECONDS * (thread_id % _HOLD_TIME_MODULUS))
            end = time.monotonic()
        finally:
            store.release(handle)
        with events_lock:
            events.append((start, end, thread_id))
        completed += 1


def _assert_no_overlapping_intervals(events: List[Tuple[float, float, int]]) -> None:
    events_sorted = sorted(events, key=lambda e: e[0])
    for i in range(1, len(events_sorted)):
        prev_start, prev_end, prev_tid = events_sorted[i - 1]
        cur_start, cur_end, cur_tid = events_sorted[i]
        assert cur_start >= prev_end, (
            f"overlap detected: thread {prev_tid} held [{prev_start:.6f}, "
            f"{prev_end:.6f}] and thread {cur_tid} held [{cur_start:.6f}, "
            f"{cur_end:.6f}] -- mutual exclusion violated"
        )


class TestLinearizabilityThreads:
    def test_many_threads_racing_for_same_key_never_overlap(self, store, unique_key):
        """Many real threads repeatedly race to acquire the SAME lock_key
        against a real PostgreSQL database. Record (start, end) intervals
        for every successful acquire/release cycle and verify NO two
        intervals overlap -- proving mutual exclusion across many trials.
        """
        operation = "add_golden_repo"
        num_threads = _LINEARIZABILITY_NUM_THREADS
        acquisitions_per_thread = _LINEARIZABILITY_ACQUISITIONS_PER_THREAD
        max_attempts_per_thread = (
            acquisitions_per_thread * _LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER
        )

        events: List[Tuple[float, float, int]] = []
        events_lock = threading.Lock()
        errors: List[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                _run_linearizability_worker(
                    store,
                    unique_key,
                    operation,
                    thread_id,
                    acquisitions_per_thread,
                    max_attempts_per_thread,
                    events,
                    events_lock,
                )
            except Exception as exc:  # noqa: BLE001
                with events_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
        for t in threads:
            assert not t.is_alive(), "worker thread failed to terminate within timeout"

        assert not errors, f"worker thread(s) raised: {errors!r}"
        assert len(events) == num_threads * acquisitions_per_thread
        _assert_no_overlapping_intervals(events)


def _run_barrier_race_worker(
    store,
    unique_key: str,
    idx: int,
    start_barrier: threading.Barrier,
    hold_barrier: threading.Barrier,
    barrier_timeout_seconds: float,
    results: List[bool],
    results_lock: threading.Lock,
) -> None:
    """One thread's single simultaneous try_acquire() attempt.

    Two barriers, deliberately: `start_barrier` releases every thread's
    ONE try_acquire() call at the same instant (this is what makes the
    race genuine). `hold_barrier` then makes every thread -- winner and
    losers alike -- wait until every thread has recorded its result
    BEFORE the winner is allowed to release. Without this second
    barrier, the winner could release immediately, letting a still
    -waiting loser's in-flight attempt succeed too (a real, empirically
    -confirmed flake given PostgreSQL's row-lock wait queue) -- which
    would make "exactly one winner" nondeterministic even under a
    correct implementation.

    Cleanup stays entirely inside this thread (no cross-thread handle
    handoff): the SAME thread that acquires the lock releases it in a
    `finally`, guaranteed even if the `hold_barrier` wait itself raises
    (e.g. `BrokenBarrierError`/`TimeoutError`) -- `results[idx]` records
    only a plain win/loss bool, never the handle itself.
    """
    start_barrier.wait(timeout=barrier_timeout_seconds)
    acquired = store.try_acquire(unique_key, operation="op")
    try:
        won = acquired is not None
        with results_lock:
            results[idx] = won

        hold_barrier.wait(timeout=barrier_timeout_seconds)
    finally:
        if acquired is not None:
            store.release(acquired)


class TestConcurrencyContentionIsGenuine:
    """Fix #6: a purely-serializing (i.e. BLOCKING) implementation trivially
    satisfies "no overlapping intervals" -- proven empirically: the
    original, broken (indefinitely-blocking) PostgresAliasLockStore
    PASSED TestLinearizabilityThreads above even though try_acquire() was
    fundamentally broken, precisely because blocking threads never
    produce an overlap to detect. This test closes that gap directly: it
    synchronizes N threads on a barrier so they all call try_acquire() on
    the SAME fresh key at the same instant, then asserts BOTH that (a)
    every thread resolves PROMPTLY (bounded time, never blocking), and
    (b) most of them genuinely observe contention (a loss) rather than
    blocking-then-succeeding. A broken blocking implementation fails
    assertion (a): the losing threads would still be alive/blocked well
    past the deadline. See ``_run_barrier_race_worker`` for the two
    -barrier design and why handle cleanup can never leak here.
    """

    def test_barrier_synchronized_race_yields_one_winner_and_prompt_losers(
        self, store, unique_key
    ):
        num_threads = _LINEARIZABILITY_NUM_THREADS
        start_barrier = threading.Barrier(num_threads)
        hold_barrier = threading.Barrier(num_threads)
        results_lock = threading.Lock()
        won_flags: List[bool] = [False] * num_threads
        errors: List[Exception] = []

        def worker(idx: int) -> None:
            try:
                _run_barrier_race_worker(
                    store,
                    unique_key,
                    idx,
                    start_barrier,
                    hold_barrier,
                    _GENEROUS_ACQUIRE_TIMEOUT_SECONDS,
                    won_flags,
                    results_lock,
                )
            except Exception as exc:  # noqa: BLE001
                with results_lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_PROMPT_CONTENTION_DEADLINE_SECONDS)
        was_alive_at_deadline = [t.is_alive() for t in threads]

        for t, alive_at_deadline in zip(threads, was_alive_at_deadline):
            assert not alive_at_deadline, (
                "a losing attempt in the barrier-synchronized race BLOCKED "
                "past the prompt-resolution deadline instead of resolving "
                "promptly -- contention must resolve to None, never an "
                "indefinite wait"
            )
            assert not t.is_alive(), "worker thread failed to terminate"

        assert not errors, f"worker thread(s) raised: {errors!r}"
        with results_lock:
            current_won_flags = list(won_flags)
        winners = sum(1 for won in current_won_flags if won)
        losers = num_threads - winners
        assert winners == 1, f"expected exactly one winner, got {winners}"
        assert losers == num_threads - 1, (
            "expected the remaining threads to genuinely observe "
            "contention (a loss), not block-then-succeed"
        )
