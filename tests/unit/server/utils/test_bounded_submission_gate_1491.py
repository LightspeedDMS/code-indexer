"""BoundedSubmissionGate slot accounting (Story #1491 AC3, review round 4).

The gate exists to stop ``fetch_discovery_branches`` handing an unbounded
number of pending ``git ls-remote`` tasks to its shared pool.  Its whole risk is
slot accounting: a slot lost to a cancellation would shrink the budget
permanently and, at capacity 1, wedge discovery outright.  These tests drive the
REAL gate on a REAL event loop -- nothing is mocked, and every cancellation is a
genuine ``Task.cancel()``.

The three cancellation paths are distinguished by exactly WHEN the cancel lands
relative to the handover, and each is landed deterministically by controlling
how many times the loop is allowed to tick in between -- no sleeps, no timing
guesses.
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from code_indexer.server.utils.bounded_submission_gate import BoundedSubmissionGate


async def _acquire_into(gate: BoundedSubmissionGate, log: List[str], tag: str) -> None:
    """Wait for a slot and record that it was granted."""
    await gate.acquire()
    log.append(tag)


async def _tick(times: int = 1) -> None:
    """Let the loop run pending callbacks without introducing real delay."""
    for _ in range(times):
        await asyncio.sleep(0)


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BoundedSubmissionGate(0)


@pytest.mark.asyncio
async def test_grants_up_to_capacity_then_makes_callers_wait() -> None:
    gate = BoundedSubmissionGate(2)
    granted: List[str] = []

    await gate.acquire()
    await gate.acquire()
    waiting = asyncio.ensure_future(_acquire_into(gate, granted, "third"))
    await _tick(2)
    assert granted == [], "the gate granted a third slot over its capacity of 2"

    gate.release()
    await _tick(2)
    assert granted == ["third"], "the freed slot was never handed to the waiter"

    gate.release()
    gate.release()
    await waiting


@pytest.mark.asyncio
async def test_slot_is_handed_over_in_arrival_order() -> None:
    gate = BoundedSubmissionGate(1)
    granted: List[str] = []

    await gate.acquire()
    first = asyncio.ensure_future(_acquire_into(gate, granted, "first"))
    await _tick(2)
    second = asyncio.ensure_future(_acquire_into(gate, granted, "second"))
    await _tick(2)

    gate.release()
    await _tick(2)
    gate.release()
    await _tick(2)

    assert granted == ["first", "second"]
    await asyncio.gather(first, second)
    gate.release()


@pytest.mark.asyncio
async def test_waiter_cancelled_while_queued_does_not_consume_the_slot() -> None:
    """A cancelled queued waiter must be skipped, not handed the slot."""
    gate = BoundedSubmissionGate(1)
    granted: List[str] = []

    await gate.acquire()
    doomed = asyncio.ensure_future(_acquire_into(gate, granted, "doomed"))
    await _tick(2)
    survivor = asyncio.ensure_future(_acquire_into(gate, granted, "survivor"))
    await _tick(2)

    doomed.cancel()
    await _tick(2)
    gate.release()
    await _tick(2)

    assert granted == ["survivor"], (
        f"the released slot went to a cancelled waiter and was lost (granted={granted})"
    )
    await survivor
    gate.release()


@pytest.mark.asyncio
async def test_cancel_racing_the_handover_callback_returns_the_slot() -> None:
    """Cancel lands AFTER release scheduled the handover, BEFORE it runs.

    ``release()`` has already committed the slot to this waiter and left
    ``_in_flight`` unchanged, so the handover callback is the only thing that
    can give it back.  If it does not, the budget shrinks permanently.
    """
    gate = BoundedSubmissionGate(1)
    granted: List[str] = []

    await gate.acquire()
    doomed = asyncio.ensure_future(_acquire_into(gate, granted, "doomed"))
    await _tick(2)
    survivor = asyncio.ensure_future(_acquire_into(gate, granted, "survivor"))
    await _tick(2)

    gate.release()  # schedules the handover to `doomed`
    doomed.cancel()  # lands before the scheduled callback runs
    await _tick(4)

    assert granted == ["survivor"], (
        "a cancellation racing the handover swallowed the slot; the gate is "
        f"now permanently short (granted={granted})"
    )
    await survivor
    gate.release()


@pytest.mark.asyncio
async def test_cancel_after_the_slot_was_delivered_returns_the_slot() -> None:
    """Cancel lands AFTER the handover completed but before the waiter resumes.

    Here the waiter's future is already resolved, so the gate cannot detect the
    loss in the callback -- ``acquire()``'s own cancellation handler must hand
    the slot on instead.
    """
    gate = BoundedSubmissionGate(1)
    granted: List[str] = []

    await gate.acquire()
    doomed = asyncio.ensure_future(_acquire_into(gate, granted, "doomed"))
    await _tick(2)
    survivor = asyncio.ensure_future(_acquire_into(gate, granted, "survivor"))
    await _tick(2)

    gate.release()
    await _tick(1)  # the handover callback runs: doomed's future is resolved
    doomed.cancel()
    await _tick(4)

    assert granted == ["survivor"], (
        "a waiter cancelled after being granted its slot never returned it "
        f"(granted={granted})"
    )
    await survivor
    gate.release()


@pytest.mark.asyncio
async def test_release_without_acquire_fails_loudly() -> None:
    """Corrupt accounting must never be papered over (Messi Rule #13)."""
    gate = BoundedSubmissionGate(1)
    with pytest.raises(RuntimeError):
        gate.release()
