"""Concurrency/linearizability tests for SqliteAliasLockStore (Issue #1546
Phase 1).

Property-style tests across many trials -- many real threads, and
separately many real OS processes -- proving mutual exclusion holds, not
a single hand-picked interleaving. No mocking of the lock mechanism.

Multi-process is exercised because SQLite in this codebase IS accessed
multi-process whenever uvicorn runs with `workers > 1` against
`storage_mode: "sqlite"` (see server/utils/config_manager.py's
`ServerConfig.workers` default of 1, which operators may raise) -- the
lock file is shared across OS processes on disk, not merely across
threads within one process.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Tuple

from code_indexer.server.services.alias_lock_store.sqlite_store import (
    SqliteAliasLockStore,
)

_GENEROUS_BUSY_TIMEOUT_SECONDS = 5.0
_THREAD_JOIN_TIMEOUT_SECONDS = 60
_LINEARIZABILITY_NUM_THREADS = 6
_LINEARIZABILITY_ACQUISITIONS_PER_THREAD = 15
_LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER = 50
_HOLD_TIME_SCALE_SECONDS = 0.001
_HOLD_TIME_MODULUS = 3
_DIFFERENT_KEYS_NUM_THREADS = 8
_MAX_ATTEMPTS_PER_WORKER = 200
_HOLD_SLEEP_SECONDS = 0.05
_POLL_SLEEP_SECONDS = 0.01
_MULTIPROCESS_NUM_TRIALS = 5
_MULTIPROCESS_NUM_PROCESSES = 2
_MULTIPROCESS_JOIN_TIMEOUT_SECONDS = 30
_MULTIPROCESS_TERMINATE_JOIN_TIMEOUT_SECONDS = 5


def _make_store(tmp_path: Path, **kwargs) -> SqliteAliasLockStore:
    return SqliteAliasLockStore(tmp_path / "alias_locks.db", **kwargs)


def _run_racing_worker(
    store: SqliteAliasLockStore,
    lock_key: str,
    operation: str,
    thread_id: int,
    acquisitions_per_thread: int,
    max_attempts_per_thread: int,
    events: List[Tuple[float, float, int]],
    events_lock: threading.Lock,
) -> None:
    """One thread's full acquire/hold/release loop, repeated until it has
    completed `acquisitions_per_thread` successful cycles, bounded by
    `max_attempts_per_thread` total attempts (including losses)."""
    completed = 0
    attempts = 0
    while completed < acquisitions_per_thread:
        attempts += 1
        if attempts > max_attempts_per_thread:
            raise AssertionError(
                f"thread {thread_id} exceeded bounded attempt budget "
                f"({max_attempts_per_thread}) without completing "
                f"{acquisitions_per_thread} acquisitions"
            )
        handle = store.try_acquire(lock_key, operation=operation)
        if handle is None:
            continue
        try:
            start = time.monotonic()
            # Tiny, deliberately variable hold time to encourage real
            # interleaving between threads.
            time.sleep(_HOLD_TIME_SCALE_SECONDS * (thread_id % _HOLD_TIME_MODULUS))
            end = time.monotonic()
        finally:
            store.release(handle)
        with events_lock:
            events.append((start, end, thread_id))
        completed += 1


def _assert_no_overlapping_intervals(events: List[Tuple[float, float, int]]) -> None:
    """Linearizability/history check: sort by start time and verify no two
    intervals overlap -- i.e. every interval's start is >= the end of the
    previous one in the sorted order."""
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
    def test_many_threads_racing_for_same_key_never_overlap(self, tmp_path):
        """Many real threads repeatedly race to acquire the SAME lock_key.
        Record (start, end) wall-clock intervals for every successful
        acquire/release cycle across ALL threads in one shared, lock
        -protected event log, then verify NO two intervals overlap --
        proving the mutual-exclusion property across many trials rather
        than one hand-picked interleaving.
        """
        store = _make_store(
            tmp_path, busy_timeout_seconds=_GENEROUS_BUSY_TIMEOUT_SECONDS
        )
        lock_key = "racing-alias"
        operation = "add_golden_repo"
        num_threads = _LINEARIZABILITY_NUM_THREADS
        acquisitions_per_thread = _LINEARIZABILITY_ACQUISITIONS_PER_THREAD
        max_attempts_per_thread = (
            acquisitions_per_thread * _LINEARIZABILITY_ATTEMPT_BUDGET_MULTIPLIER
        )

        events: List[Tuple[float, float, int]] = []
        events_lock = threading.Lock()
        errors: List[BaseException] = []

        def worker(thread_id: int) -> None:
            try:
                _run_racing_worker(
                    store,
                    lock_key,
                    operation,
                    thread_id,
                    acquisitions_per_thread,
                    max_attempts_per_thread,
                    events,
                    events_lock,
                )
            except BaseException as exc:  # noqa: BLE001 - must not be swallowed
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

    def test_many_threads_racing_for_different_keys_all_eventually_succeed(
        self, tmp_path
    ):
        """Sanity companion to the same-key test: racing on DIFFERENT keys
        must not deadlock or spuriously fail -- every thread's single
        acquisition on its own key eventually succeeds despite SQLite's
        whole-file serialization (see the module docstring in
        sqlite_store.py)."""
        store = _make_store(
            tmp_path, busy_timeout_seconds=_GENEROUS_BUSY_TIMEOUT_SECONDS
        )
        num_threads = _DIFFERENT_KEYS_NUM_THREADS
        results: List[bool] = [False] * num_threads
        errors: List[BaseException] = []
        results_lock = threading.Lock()

        def worker(thread_id: int) -> None:
            try:
                for _ in range(_MAX_ATTEMPTS_PER_WORKER):
                    handle = store.try_acquire(f"alias-{thread_id}", operation="op")
                    if handle is not None:
                        try:
                            pass
                        finally:
                            store.release(handle)
                        with results_lock:
                            results[thread_id] = True
                        return
                raise AssertionError(
                    f"thread {thread_id} never acquired its own independent key "
                    f"within {_MAX_ATTEMPTS_PER_WORKER} attempts"
                )
            except BaseException as exc:  # noqa: BLE001
                with results_lock:
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
        assert all(results), f"not all threads succeeded: {results}"


def _acquire_hold_release_shared_clock(
    db_path: str, lock_key: str, hold_seconds: float, out
) -> None:
    """Module-level (picklable) target for multiprocessing.Process. Uses
    time.time() (a wall clock shared across processes on the same host,
    unlike time.monotonic()) so overlap can be checked directly across
    process boundaries."""
    import time as _time

    from code_indexer.server.services.alias_lock_store.sqlite_store import (
        SqliteAliasLockStore as _Store,
    )

    store = _Store(db_path, busy_timeout_seconds=_GENEROUS_BUSY_TIMEOUT_SECONDS)
    for _ in range(_MAX_ATTEMPTS_PER_WORKER):
        handle = store.try_acquire(lock_key, operation="op")
        if handle is not None:
            try:
                start = _time.time()
                _time.sleep(hold_seconds)
                end = _time.time()
            finally:
                store.release(handle)
            out.append((start, end))
            return
        _time.sleep(_POLL_SLEEP_SECONDS)
    raise AssertionError(
        f"never acquired {lock_key!r} within {_MAX_ATTEMPTS_PER_WORKER} attempts"
    )


def _run_multiprocess_trial(db_path: str, lock_key: str, events) -> List:
    """Spawn _MULTIPROCESS_NUM_PROCESSES real OS processes racing for
    lock_key, join them with a bounded timeout, and terminate (never
    leak) any process that fails to finish in time."""
    import multiprocessing

    procs = [
        multiprocessing.Process(
            target=_acquire_hold_release_shared_clock,
            args=(db_path, lock_key, _HOLD_SLEEP_SECONDS, events),
        )
        for _ in range(_MULTIPROCESS_NUM_PROCESSES)
    ]
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=_MULTIPROCESS_JOIN_TIMEOUT_SECONDS)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=_MULTIPROCESS_TERMINATE_JOIN_TIMEOUT_SECONDS)
    return procs


class TestMultiProcessContention:
    def test_two_processes_racing_same_key_shared_clock_never_overlap(self, tmp_path):
        """Real OS processes (not just threads) racing for the same
        lock_key via the SAME dedicated alias_locks.db file, proving the
        mutual-exclusion guarantee holds across process boundaries -- the
        realistic contention scenario for a multi-worker uvicorn
        deployment against SQLite storage. Overlap is checked directly
        via a shared wall clock recorded through a multiprocessing
        Manager list. Every process is joined with a bounded timeout and
        explicitly terminated (never left running) if it somehow hangs."""
        import multiprocessing

        db_path = str(tmp_path / "alias_locks.db")
        lock_key = "cross-process-alias"
        SqliteAliasLockStore(db_path)  # pre-create schema

        with multiprocessing.Manager() as manager:
            for _ in range(_MULTIPROCESS_NUM_TRIALS):
                events = manager.list()
                procs = _run_multiprocess_trial(db_path, lock_key, events)

                for p in procs:
                    assert not p.is_alive(), "subprocess failed to terminate"
                    assert p.exitcode == 0, f"subprocess failed: exitcode={p.exitcode}"

                assert len(events) == _MULTIPROCESS_NUM_PROCESSES, (
                    f"expected {_MULTIPROCESS_NUM_PROCESSES} completions, "
                    f"got {list(events)}"
                )
                (s1, e1), (s2, e2) = sorted(events, key=lambda e: e[0])
                assert s2 >= e1, (
                    f"cross-process overlap detected: [{s1}, {e1}] and [{s2}, {e2}]"
                )
