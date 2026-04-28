"""
Story #917 — Per-Domain-File Advisory Lock for Branch A/B Concurrency.

Tests for the lock infrastructure placed in dep_map_repair_phase37.py:
  - get_domain_file_lock(domain) -> threading.Lock
  - acquire_domain_lock(domain_name, timeout_seconds) -> context manager
  - Action.domain_lock_timeout enum member

AC1: Different-domain locks do not block each other (parallelism).
AC2: Same-domain lock serializes concurrent writers.
AC3: Timeout raises TimeoutError with informative message.
AC4: Uncontended acquisition completes quickly.
AC5: Exception inside lock context releases the lock.
AC6: Action.domain_lock_timeout is present in the enum.
AC7: Empty domain name raises AssertionError.
AC8: Same lock instance returned for the same domain (singleton map).
"""

import queue
import threading
import time

import pytest

from code_indexer.server.services.dep_map_repair_phase37 import (
    Action,
    acquire_domain_lock,
    get_domain_file_lock,
)

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------
# HOLD_TIME_S: how long each thread holds the lock in AC1/AC2.
# PARALLEL_BUDGET_S: maximum wall-clock time for two parallel lock-holders.
#   Set to 2× HOLD_TIME so full serialization (which costs ≥ 2×HOLD_TIME plus
#   scheduling overhead) would exceed the budget, while true parallelism easily
#   fits. The 2× multiplier provides sufficient headroom for barrier, scheduling,
#   and join overhead.
# TIMEOUT_BUDGET_S: AC3 — how long acquire_domain_lock waits before raising.
# UNCONTENDED_BUDGET_S: AC4 — upper-bound for an uncontended acquire; 50ms is
#   a generous ceiling that still catches broken blocking code.
# REACQUIRE_TIMEOUT_S: AC5 — after exception the lock must be free; 100ms budget.
HOLD_TIME_S: float = 0.05
PARALLEL_BUDGET_S: float = (
    HOLD_TIME_S * 1.8
)  # ~0.09s — fits parallel, not serial (2×=0.10s)
TIMEOUT_BUDGET_S: float = 0.05
UNCONTENDED_BUDGET_S: float = 0.05
REACQUIRE_TIMEOUT_S: float = 0.10


def _raise_if_thread_errors(exc_queue: queue.Queue) -> None:
    """Re-raise the first captured thread exception in the main test thread."""
    if not exc_queue.empty():
        raise exc_queue.get_nowait()


# ---------------------------------------------------------------------------
# AC1: Different-domain locks do not block each other
# ---------------------------------------------------------------------------


def test_different_domain_locks_do_not_block():
    """Two threads acquiring different domain locks proceed concurrently."""
    results: queue.Queue = queue.Queue()
    exceptions: queue.Queue = queue.Queue()
    barrier = threading.Barrier(2)

    def write_domain(name: str) -> None:
        try:
            barrier.wait()  # both threads start lock acquisition simultaneously
            with acquire_domain_lock(name):
                time.sleep(HOLD_TIME_S)
                results.put(name)
        except Exception as exc:  # noqa: BLE001
            exceptions.put(exc)

    start = time.monotonic()
    t1 = threading.Thread(target=write_domain, args=("billing-ac1",))
    t2 = threading.Thread(target=write_domain, args=("auth-ac1",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.monotonic() - start

    _raise_if_thread_errors(exceptions)

    # Parallel: both threads hold different locks at the same time.
    # Full serialization would take ≥ 2×HOLD_TIME; PARALLEL_BUDGET_S is 2×HOLD_TIME
    # so a serialized implementation that barely squeezes under will not pass
    # given scheduling overhead.
    assert elapsed < PARALLEL_BUDGET_S, (
        f"Expected parallel execution (<{PARALLEL_BUDGET_S:.2f}s), got {elapsed:.3f}s"
    )
    collected = set()
    while not results.empty():
        collected.add(results.get_nowait())
    assert collected == {"billing-ac1", "auth-ac1"}


# ---------------------------------------------------------------------------
# AC2: Same-domain lock serializes concurrent writers
# ---------------------------------------------------------------------------


def test_same_domain_locks_serialize():
    """Two threads acquiring the same domain lock execute sequentially."""
    order: queue.Queue = queue.Queue()
    exceptions: queue.Queue = queue.Queue()
    barrier = threading.Barrier(2)

    def write_domain(label: str) -> None:
        try:
            barrier.wait()  # ensure both threads attempt acquisition at the same time
            with acquire_domain_lock("billing-ac2"):
                order.put(f"start-{label}")
                time.sleep(HOLD_TIME_S / 2)
                order.put(f"end-{label}")
        except Exception as exc:  # noqa: BLE001
            exceptions.put(exc)

    t1 = threading.Thread(target=write_domain, args=("A",))
    t2 = threading.Thread(target=write_domain, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    _raise_if_thread_errors(exceptions)

    events = []
    while not order.empty():
        events.append(order.get_nowait())

    # Must not interleave: the second thread's start must follow the first thread's end.
    assert len(events) == 4, f"Expected 4 events, got: {events}"
    assert events[1].startswith("end-"), f"Expected end-* at index 1, got: {events}"
    assert events[2].startswith("start-"), f"Expected start-* at index 2, got: {events}"


# ---------------------------------------------------------------------------
# AC3: Timeout raises TimeoutError with informative message
# ---------------------------------------------------------------------------


def test_lock_timeout_raises():
    """Attempting to acquire a held lock beyond timeout raises TimeoutError."""
    lock = get_domain_file_lock("slow-domain-ac3")
    lock.acquire()  # hold the lock externally
    try:
        with pytest.raises(
            TimeoutError,
            match="domain lock acquisition timed out: slow-domain-ac3",
        ):
            with acquire_domain_lock(
                "slow-domain-ac3", timeout_seconds=TIMEOUT_BUDGET_S
            ):
                pass  # pragma: no cover
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# AC4: Uncontended acquisition completes quickly
# ---------------------------------------------------------------------------


def test_uncontended_lock_is_fast():
    """Uncontended acquire_domain_lock completes within a generous upper bound."""
    start = time.monotonic()
    with acquire_domain_lock("fast-domain-ac4"):
        pass
    elapsed = time.monotonic() - start
    assert elapsed < UNCONTENDED_BUDGET_S, (
        f"Expected <{UNCONTENDED_BUDGET_S * 1000:.0f}ms, got {elapsed * 1000:.2f}ms"
    )


# ---------------------------------------------------------------------------
# AC5: Exception inside lock releases it
# ---------------------------------------------------------------------------


def test_lock_released_on_exception():
    """Exception raised inside acquire_domain_lock context releases the lock."""
    with pytest.raises(RuntimeError, match="simulated failure"):
        with acquire_domain_lock("exc-domain-ac5"):
            raise RuntimeError("simulated failure")

    # Lock must be released — immediate re-acquire must succeed within generous budget.
    lock = get_domain_file_lock("exc-domain-ac5")
    acquired = lock.acquire(timeout=REACQUIRE_TIMEOUT_S)
    assert acquired, "Lock should have been released after exception"
    lock.release()


# ---------------------------------------------------------------------------
# AC6: domain_lock_timeout in Action enum
# ---------------------------------------------------------------------------


def test_action_enum_has_domain_lock_timeout():
    """Action enum must contain domain_lock_timeout with correct value."""
    assert hasattr(Action, "domain_lock_timeout"), (
        "Action enum is missing domain_lock_timeout member"
    )
    assert Action.domain_lock_timeout.value == "domain_lock_timeout"


# ---------------------------------------------------------------------------
# AC7: Empty domain name raises AssertionError
# ---------------------------------------------------------------------------


def test_empty_domain_name_raises():
    """acquire_domain_lock with empty string must raise ValueError."""
    with pytest.raises(ValueError):
        with acquire_domain_lock(""):
            pass  # pragma: no cover


# ---------------------------------------------------------------------------
# AC8: Lock map is singleton — same instance returned for same domain
# ---------------------------------------------------------------------------


def test_same_lock_instance_returned_for_same_domain():
    """get_domain_file_lock returns the identical threading.Lock for the same name."""
    lock1 = get_domain_file_lock("shared-ac8")
    lock2 = get_domain_file_lock("shared-ac8")
    assert lock1 is lock2, (
        "Expected same Lock instance for repeated calls with same domain"
    )
