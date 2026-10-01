"""
Bug #1959: 61 tests in tests/unit/server/auth/ fail when the suite runs
after tests/unit/server/web/ in the same pytest process, all with:

    RuntimeError: There is no current event loop in thread 'MainThread'.

Root cause: tests/unit/server/web/test_omni_search_dead_code_removed_1753.py
calls `asyncio.run(...)` directly in the MainThread. CPython's
`asyncio.run()` unconditionally calls `asyncio.set_event_loop(None)` in its
`finally` block (asyncio/runners.py) once the coroutine completes -- this
leaves the thread's "current event loop" EXPLICITLY unset, which is a
DIFFERENT state from "never set": the default event-loop policy only
auto-creates a loop lazily in the latter case. Any later test that calls
the deprecated bare `asyncio.get_event_loop()` (three files under
`tests/unit/server/auth/`: test_timing_attack_async.py,
test_mcp_auth_off_event_loop_1491.py,
test_oauth_mcp_pre_elevation_v10_4_7.py; several more under
tests/unit/server/mcp/, middleware/, routers/, startup/ -- see the audit
note in `tests/unit/server/conftest.py`) then hits the RuntimeError instead
of getting a lazily created loop.

This is the same recurring shape as Bug #1694 (app.state leak) and Bug
#1635 (BackgroundJobManager thread leak): one directory's real integration
work mutates thread/process-global interpreter state that pytest does not
reset between tests, and a later, unrelated directory silently depends on
whatever state happened to be left behind. The fix is the tree-wide
autouse fixture `_restore_event_loop_after_asyncio_run` in
`tests/unit/server/conftest.py`, which guarantees `asyncio.get_event_loop()`
stays usable in the MainThread both before and after every test under
tests/unit/server/, regardless of what ran immediately before it.

IMPORTANT re: what is "the system under test" here, mirroring the
established rationale in `test_app_state_leak_protection_1694.py` and
`test_background_job_manager_universal_teardown_1635.py`: the function
under test, `_restore_event_loop_impl`, IS the conftest.py
fixture-generator itself. Driving it directly via `next()` (rather than
only observing it indirectly through pytest's fixture machinery) lets this
test assert its exact repair semantics against the REAL thread-global
asyncio event-loop state, with no test double standing in for either the
generator or asyncio itself, and without needing a second pytest process
to prove order-independence.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.server.conftest import _restore_event_loop_impl


def test_nulled_event_loop_is_repaired_after_teardown() -> None:
    """Directly reproduce Bug #1959's exact failure mode, then prove the
    fixture's teardown phase repairs it.

    Discriminating input: `asyncio.set_event_loop(None)` is EXACTLY what
    `asyncio.run()`'s cleanup does (verified against cpython's
    asyncio/runners.py `run()` finally block) -- not a stand-in for some
    other kind of breakage. A naive fix that merely swallows the
    RuntimeError without leaving a real, usable, non-closed loop behind
    would fail the final assertions below.
    """
    gen = _restore_event_loop_impl()
    next(gen)  # setup phase: fixture's own pre-test guarantee runs first

    # Simulate the exact leak: another directory's test called asyncio.run()
    # directly in the MainThread, whose cleanup explicitly unset the loop.
    asyncio.set_event_loop(None)

    # RED: reproduce the reported bug's exact error before any repair --
    # this is the state tests/unit/server/auth/ observes today when it
    # runs after tests/unit/server/web/.
    with pytest.raises(RuntimeError, match="no current event loop"):
        asyncio.get_event_loop()

    # Drive the generator's teardown phase (mirrors the established
    # next()-until-StopIteration pattern for these tree-wide fixtures).
    with pytest.raises(StopIteration):
        next(gen)

    # GREEN: repaired -- the deprecated asyncio.get_event_loop() call that
    # tests/unit/server/auth/'s three affected files still make must not
    # raise, and must return a real, usable, non-closed loop.
    loop = asyncio.get_event_loop()
    assert loop is not None, (
        "Bug #1959: asyncio.get_event_loop() returned None after the "
        "event-loop-restore fixture's teardown ran -- the leak was not "
        "repaired."
    )
    assert not loop.is_closed(), (
        "Bug #1959: the event loop left in place after the fixture's "
        "teardown is closed -- callers of the deprecated "
        "asyncio.get_event_loop() would still fail to use it."
    )
