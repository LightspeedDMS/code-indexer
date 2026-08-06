"""Story #1491 (dual-review item 3): the gate's OWN waiter queue must be bounded.

Bounding how many units execute concurrently, while letting the queue of
pending callers grow without limit, does not bound anything that matters -- it
relocates the unbounded memory growth from the executor's internal
``SimpleQueue`` into the gate's own ``_waiters`` deque. A live probe with 1000
concurrent callers produced 1000 queued waiters, which is the defect these
tests pin closed.

Genuine backpressure: a caller arriving when the queue is already at its bound
is REJECTED immediately (``SubmissionGateOverloadedError``) rather than parked.
"""

from __future__ import annotations

import asyncio

import pytest

from code_indexer.server.utils.bounded_submission_gate import (
    BoundedSubmissionGate,
    SubmissionGateOverloadedError,
)

# Deliberately far more callers than capacity + max_waiters, mirroring the
# 1000-caller probe that exposed the unbounded growth.
_CONCURRENT_CALLERS = 1000
_CAPACITY = 4
_MAX_WAITERS = 16


@pytest.mark.asyncio
async def test_waiter_queue_never_exceeds_its_bound_under_heavy_load() -> None:
    """1000 concurrent callers must not produce 1000 queued waiters."""
    gate = BoundedSubmissionGate(capacity=_CAPACITY, max_waiters=_MAX_WAITERS)
    peak_waiters = 0
    admitted = 0
    rejected = 0

    async def _caller() -> None:
        nonlocal peak_waiters, admitted, rejected
        try:
            await gate.acquire()
        except SubmissionGateOverloadedError:
            rejected += 1
            return
        admitted += 1
        peak_waiters = max(peak_waiters, gate.waiter_count)
        # Yield so other callers genuinely interleave before the slot frees.
        await asyncio.sleep(0)
        gate.release()

    await asyncio.gather(*[_caller() for _ in range(_CONCURRENT_CALLERS)])

    assert peak_waiters <= _MAX_WAITERS, (
        f"waiter queue reached {peak_waiters}, exceeding its bound of "
        f"{_MAX_WAITERS} -- the unbounded growth was only relocated"
    )
    assert gate.waiter_count == 0, "waiters leaked after every caller finished"
    assert admitted + rejected == _CONCURRENT_CALLERS
    assert admitted > 0, "no caller was ever admitted -- the gate is not working"


@pytest.mark.asyncio
async def test_caller_arriving_at_a_full_queue_is_rejected_not_parked() -> None:
    """The overload signal must be raised, not silently absorbed."""
    gate = BoundedSubmissionGate(capacity=1, max_waiters=1)

    # Fill the single slot, then the single waiter position.
    await gate.acquire()
    parked = asyncio.ensure_future(gate.acquire())
    await asyncio.sleep(0)
    assert gate.waiter_count == 1

    with pytest.raises(SubmissionGateOverloadedError):
        await gate.acquire()

    # The legitimately-parked caller is unaffected by the rejection and still
    # receives its slot when one frees.
    gate.release()
    await parked
    gate.release()
    assert gate.waiter_count == 0


def test_max_waiters_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        BoundedSubmissionGate(capacity=1, max_waiters=-1)
