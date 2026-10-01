"""Bug #1822 follow-up (c): ``tests/unit/xray/conftest.py``'s autouse
``cleanup_event_loop`` fixture (Bug #1817) must never clobber a test that
fully manages its own event loop lifecycle end to end (create, set, use,
close -- all within the test body itself).

This file proves that empirically with two tests:

1. A test that creates its own new loop, sets it via
   ``asyncio.set_event_loop``, runs a trivial coroutine on it, and closes it
   itself before returning -- mimicking a legitimate test that owns its own
   loop lifecycle. If the autouse fixture's teardown were to raise (e.g. a
   double-close propagating an exception), pytest would report this test as
   an ERROR, not a pass -- so the test simply passing is itself the primary
   evidence.

2. A SEPARATE test proving the fixture leaves clean state for "whatever
   runs next": it asserts that, after test 1's fixture teardown ran,
   ``asyncio.get_event_loop()`` returns a fresh, OPEN loop -- even though
   test 1 had already closed its own loop itself.

No functional bug was found in the fixture as shipped -- this is a
coverage-only follow-up, per the task's explicit instruction not to "fix" a
bug that does not exist. The task requires PROVING correctness empirically
rather than asserting it from documentation/reasoning; the evidence gathered
while writing this file is recorded below.

RED/GREEN evidence (recorded 2026-09-08, via a throwaway ``python3 -c``
script, not asserted from documentation):

- ``BaseEventLoop.close()`` is a documented no-op when the loop is already
  closed -- verified directly: closing an already-closed loop raises
  nothing. ``stop()`` and ``is_running()`` on an already-closed loop also
  raise nothing. So the fixture's own ``if not loop.is_closed(): loop.close()``
  guard is defensive but NOT load-bearing against a *raised exception* on
  double-close -- breaking that specific guard would not make either test
  below fail.
- The genuinely load-bearing statement is the fixture's UNCONDITIONAL final
  line, ``asyncio.set_event_loop(asyncio.new_event_loop())``: Python's
  deprecated ``asyncio.get_event_loop()`` does NOT auto-create a fresh loop
  merely because the previously-set one is closed -- verified directly: after
  ``set_event_loop(loop)`` followed by ``loop.close()``,
  ``asyncio.get_event_loop()`` returns THE SAME closed loop object, not a
  new one.
- To turn that fact into a genuine discriminating RED/GREEN cycle for THIS
  test file (not just the throwaway script), the fixture's final line in
  ``tests/unit/xray/conftest.py`` was temporarily commented out and this
  file was run:
      pytest tests/unit/xray/test_event_loop_fixture_interaction_1822.py -q
  RED result: ``test_fresh_open_loop_available_after_prior_test_closed_its_own_loop``
  FAILED with
      AssertionError: expected a fresh, OPEN event loop after the prior
      test's self-managed loop was closed and the autouse fixture's
      teardown ran -- got a CLOSED loop instead
  while the self-managed-loop test still passed (confirming the guard
  itself was never the discriminating line). The commented-out line was
  then restored immediately, and a re-run confirmed both tests pass again
  (GREEN) -- see the task's final report for the exact re-run output.
"""

from __future__ import annotations

import asyncio

EXPECTED_COROUTINE_RESULT = 42


async def _trivial_coro() -> int:
    return EXPECTED_COROUTINE_RESULT


class TestEventLoopFixtureDoesNotClobberSelfManagedLoop:
    def test_self_managed_loop_lifecycle_completes_without_fixture_interference(
        self,
    ):
        """Create, set, use, and close an event loop entirely within this
        test body. If the autouse cleanup_event_loop fixture's teardown
        were to raise while handling the already-closed loop this test
        leaves behind, pytest would report this test as an ERROR (not a
        pass) -- so this test passing cleanly is itself the primary
        evidence. The explicit checks below are secondary, direct
        confirmation of the same fact.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(_trivial_coro())
            assert result == EXPECTED_COROUTINE_RESULT
        finally:
            loop.close()

        assert loop.is_closed(), "the test's own loop.close() call did not close it"


class TestEventLoopFixtureRestoresCleanStateForWhateverRunsNext:
    def test_fresh_open_loop_available_after_prior_test_closed_its_own_loop(self):
        """A SEPARATE test proving the autouse fixture's teardown ran to
        completion after the previous test above (which already closed its
        own loop). asyncio.get_event_loop() must return a FRESH, OPEN loop
        -- proving the fixture correctly repaired state instead of leaving
        the prior test's closed loop in place.

        This assertion is not a documentation assumption: as recorded in
        this module's docstring, asyncio.get_event_loop() does NOT
        auto-create a new loop merely because the previously-set one was
        closed -- it returns the same closed object. So this assertion
        genuinely depends on the fixture's own unconditional final
        ``asyncio.set_event_loop(asyncio.new_event_loop())`` call having
        run (empirically confirmed via the RED/GREEN cycle described in the
        module docstring).
        """
        loop = asyncio.get_event_loop()
        assert not loop.is_closed(), (
            "expected a fresh, OPEN event loop after the prior test's "
            "self-managed loop was closed and the autouse fixture's "
            "teardown ran -- got a CLOSED loop instead"
        )
