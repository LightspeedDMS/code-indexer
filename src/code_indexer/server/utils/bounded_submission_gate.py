"""Admission gate for work handed to a shared thread pool.

Story #1491 (AC3, dual-review round 4).  A ``ThreadPoolExecutor`` bounds how
many tasks RUN concurrently, but its internal work queue is an unbounded
``concurrent.futures.thread.SimpleQueue``: everything submitted beyond
``max_workers`` is accepted and buffered without limit.  For
``fetch_discovery_branches`` that meant one large auto-discovery request (or
several concurrent ones) could enqueue an unlimited number of pending
``git ls-remote`` tasks -- memory growth with no ceiling and no signal.

This module supplies the missing admission control as a reusable mechanism: a
caller ``acquire()``s a slot before submitting and ``release()``s it when the
work finishes, so the number of tasks OUTSTANDING in the pool never exceeds
``capacity``.

**Ownership of the instance is the caller's, and it must be a single shared
one.**  This file deliberately defines no singleton: the bound belongs to the
pool it governs, so the one live instance is created and held next to that pool
by the integration point -- ``_DISCOVERY_BRANCH_FETCH_GATE`` in
``server/web/routes.py``, module-level exactly like the discovery pool itself.
A gate constructed per request would bound one request and nothing else (review
item 11, the same defect already fixed for the pool).

Two further properties are deliberate and load-bearing:

* **Never blocks the event loop, never parks a thread.**  Waiting happens on an
  ``asyncio.Future``, so an over-budget caller costs one suspended coroutine --
  not a blocked loop (which is the very defect this story exists to remove) and
  not a hostage worker thread (which would starve every other
  ``run_in_executor`` caller in the process).
* **Cross-loop safe.**  Each waiter's future is created on, and resumed via
  ``call_soon_threadsafe`` on, its OWN loop.  An ``asyncio.Semaphore`` cannot be
  used here for exactly this reason: it binds to the first loop that awaits it,
  so a module-level one breaks the moment a second loop exists in the process
  (tests, or any loop-per-thread work).

Scope note, stated honestly: this bounds work handed to the POOL.  The number of
suspended coroutines still scales with the caller's own request size -- that is
the request payload's inherent cost, and each waiter holds no thread, no queue
slot and no subprocess.  Rejecting oversized requests outright is a separate
policy decision and is deliberately not made here.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Deque, Tuple


class SubmissionGateOverloadedError(RuntimeError):
    """Raised when both the slot budget AND the waiter queue are full.

    This is deliberate backpressure, not an internal error: the caller is being
    told to shed load rather than being parked in a queue that would otherwise
    grow without bound.
    """


# Default waiter-queue bound. Beyond this many callers already parked, a new
# caller is rejected rather than queued -- bounding only concurrent execution
# while letting the queue grow without limit bounds nothing that matters.
_DEFAULT_MAX_WAITERS = 64


class BoundedSubmissionGate:
    """Cap the number of concurrently outstanding units of work.

    Slot accounting has exactly three transitions, all under ``self._lock``:
    an acquire below capacity increments the holder count; a release with a
    live waiter TRANSFERS the slot (count unchanged); a release with no live
    waiter decrements.  Cancellation never strands a slot: a waiter cancelled
    while queued is skipped by ``release`` and a waiter cancelled after the
    handover was scheduled returns the slot -- from ``_deliver_slot`` if the
    cancellation landed first, from ``acquire``'s own handler if it landed
    after the result was set.
    """

    def __init__(self, capacity: int, max_waiters: int = _DEFAULT_MAX_WAITERS) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        if max_waiters < 0:
            raise ValueError(f"max_waiters must be >= 0, got {max_waiters}")
        self._capacity = capacity
        self._max_waiters = max_waiters
        self._lock = threading.Lock()
        self._in_flight = 0
        self._waiters: Deque[
            Tuple[asyncio.AbstractEventLoop, "asyncio.Future[None]"]
        ] = deque()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def max_waiters(self) -> int:
        return self._max_waiters

    @property
    def waiter_count(self) -> int:
        """Current queue depth. Exposed so a test can prove the bound holds."""
        with self._lock:
            return len(self._waiters)

    async def acquire(self) -> None:
        """Take a slot, or fail fast when the queue is already at its bound.

        The waiter queue is CAPPED. Bounding only the number of concurrently
        executing units while letting the queue of pending callers grow without
        limit does not bound anything that matters -- it relocates the
        unbounded memory growth from the executor's internal queue into this
        deque. Genuine backpressure means a caller arriving at a full queue is
        REJECTED immediately rather than parked indefinitely.

        Raises:
            SubmissionGateOverloadedError: the queue is at ``max_waiters``.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._in_flight < self._capacity:
                self._in_flight += 1
                return
            if len(self._waiters) >= self._max_waiters:
                raise SubmissionGateOverloadedError(
                    f"submission gate overloaded: {self._in_flight} in flight at "
                    f"capacity {self._capacity}, and the waiter queue is full at "
                    f"its bound of {self._max_waiters}"
                )
            waiter: "asyncio.Future[None]" = loop.create_future()
            self._waiters.append((loop, waiter))

        try:
            await waiter
        except asyncio.CancelledError:
            # Cancelled AFTER the slot was delivered: it is already ours and
            # would otherwise leak, so pass it straight to the next waiter.
            if waiter.done() and not waiter.cancelled():
                self.release()
            raise

    def release(self) -> None:
        """Return a slot, handing it to the longest-waiting caller if any.

        Safe to call from any thread -- the discovery route releases from the
        event loop, but a completion callback running on a pool worker is an
        equally valid caller.
        """
        with self._lock:
            while self._waiters:
                loop, waiter = self._waiters.popleft()
                if waiter.cancelled():
                    continue
                try:
                    loop.call_soon_threadsafe(self._deliver_slot, waiter)
                except RuntimeError:
                    # That waiter's loop is closed; it can never consume the
                    # slot, so offer it to the next one instead.
                    continue
                # Slot transferred: _in_flight is unchanged because the number
                # of outstanding holders did not change.  Delivery is
                # guaranteed to conclude in _deliver_slot, which returns the
                # slot if the waiter turns out to have been cancelled.
                return
            if self._in_flight < 1:
                raise RuntimeError(
                    "BoundedSubmissionGate.release() called more times than "
                    "acquire() -- the slot accounting is corrupt"
                )
            self._in_flight -= 1

    def _deliver_slot(self, waiter: "asyncio.Future[None]") -> None:
        """Complete the handover, or return the slot if the waiter is gone.

        Runs on the waiter's own loop thread, so no cancellation can interleave
        between the check and the ``set_result``.
        """
        if waiter.cancelled():
            self.release()
            return
        waiter.set_result(None)
