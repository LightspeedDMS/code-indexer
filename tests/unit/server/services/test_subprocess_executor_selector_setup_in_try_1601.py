"""Issue #1601 remediation round 5, Priority 3 (both round-4 reviewers
independently found this same 3-line issue).

``_wait_with_output_cap`` builds its ``selectors.DefaultSelector()`` and
registers both ``process.stdout``/``process.stderr`` with it BEFORE the
``try`` block that carries the cleanup guarantee (terminate-on-failure in
``except BaseException``, guaranteed stream-close in ``finally``).

``DefaultSelector()`` allocates a real epoll fd; ``register()`` can also
fail (e.g. under fd exhaustion / EMFILE -- exactly the condition the
Priority-1 fd-leak fix from round 4 exists to guard against). If either
call raises before the ``try`` block is entered, the already-spawned real
child process is NEVER terminated and its pipes are NEVER closed -- the
cleanup guarantee that ``test_subprocess_executor_fd_leak_1601.py`` proved
for every OTHER failure point in this method does not cover this one.

This test proves the gap with a REAL child process (spawned via the real,
unmocked ``subprocess.Popen`` -- only observationally wrapped to capture
the real PID, exactly like the established pattern in
``test_subprocess_executor_reaping_1601.py`` and
``test_subprocess_executor_fd_leak_1601.py``) and a selector wrapper that
raises on its SECOND ``register()`` call (simulating a registration
failure for stderr after stdout succeeded, e.g. an EMFILE-style error)
-- proving the real child is terminated and reaped rather than leaked
running.

The child script is bounded (a finite ``time.sleep(30)``) so it always
has a provable termination path even if the test's own assertions fail;
a single test-side ``try/finally`` wraps BOTH the awaited execution call
and the assertions, so a real leaked process/selector is force-cleaned up
regardless of whether ``execute_with_limits`` raises outright or the
assertions themselves fail -- a RED run never leaks a live process or an
open epoll fd for the rest of the suite.
"""

from __future__ import annotations

import selectors
import subprocess
import sys
from unittest.mock import patch

import psutil
import pytest

from code_indexer.server.services.subprocess_executor import SubprocessExecutor

_TEST_MAX_OUTPUT_BYTES = 8192
_EXECUTION_TIMEOUT_SECONDS = 30

# A real, finite (bounded) child -- proves TERMINATION rather than a
# coincidental natural exit, since the test asserts on process state
# immediately after execute_with_limits returns, well before this sleep
# would complete on its own.
_SLEEPY_SCRIPT = "import time\ntime.sleep(30)\n"

# Captured BEFORE the patch below is applied. The patch target
# (``subprocess_executor.selectors.DefaultSelector``) is the identical
# module attribute this test file's own ``selectors`` import sees --
# calling ``selectors.DefaultSelector()`` from inside the wrapper below
# (after the patch is active) would recurse into its own side_effect
# forever, so the real class is captured here and used directly instead.
_REAL_DEFAULT_SELECTOR = selectors.DefaultSelector


class _FailingSecondRegisterSelector:
    """Wraps a real DefaultSelector but raises OSError (EMFILE-shaped) on
    its SECOND register() call -- simulating a real registration failure
    encountered mid-setup (stdout succeeds, stderr fails), while still
    delegating every other operation to a real, working selector so the
    rest of the supervision loop behaves normally if it ever gets there.

    Allocates a REAL selector (a real epoll fd on Linux) -- the test that
    constructs this must retain the instance and ``close()`` it itself,
    since the whole point of this test is that production code's OWN
    cleanup of this object is what's under test (and, pre-fix, does not
    happen)."""

    def __init__(self) -> None:
        self._real = _REAL_DEFAULT_SELECTOR()
        self._register_calls = 0

    def register(self, fileobj, events, data=None):
        self._register_calls += 1
        if self._register_calls == 2:
            raise OSError(24, "Too many open files")
        return self._real.register(fileobj, events, data)

    def unregister(self, fileobj):
        return self._real.unregister(fileobj)

    def select(self, timeout=None):
        return self._real.select(timeout)

    def close(self):
        return self._real.close()


def _capture_popen_factory(captured: dict):
    real_popen = subprocess.Popen

    def _capture_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured["process"] = proc
        return proc

    return _capture_popen


def _capture_selector_factory(captured: dict):
    def _make() -> _FailingSecondRegisterSelector:
        instance = _FailingSecondRegisterSelector()
        captured["selector"] = instance
        return instance

    return _make


def _cleanup_leaked_process(process) -> None:
    """Test-side safety net (never relied on to PROVE the fix -- the
    assertions do that): if the production code under test failed to
    terminate the real child, don't let a RED run leak a live process for
    the rest of the suite.

    ``ProcessLookupError``/``OSError`` here mean the process already
    exited (or was already reaped) by the time this runs -- an expected,
    harmless race with the exact production cleanup this test is
    exercising, not a swallowed real failure. ``TimeoutExpired`` after a
    SIGKILL would indicate a genuinely stuck process; intentionally not
    escalated further since this is best-effort test cleanup, not the
    behavior under test.
    """
    if process is None:
        return
    try:
        if psutil.pid_exists(process.pid):
            process.kill()
            process.wait(timeout=5)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        pass


def _cleanup_leaked_selector(selector) -> None:
    """Test-side safety net for the real epoll fd ``_FailingSecondRegisterSelector``
    allocates -- closing an already-closed selector is a documented no-op
    in the ``selectors`` module, so no exception handling is needed here."""
    if selector is not None:
        selector.close()


def _assert_child_terminated_and_streams_closed(result, process) -> None:
    """The injected registration failure must surface as an ERROR result
    (not silently swallowed, not a hang), and the real child must be
    terminated/reaped with both pipes closed -- not left running/leaked
    because the failure happened "too early" to be covered by the
    cleanup guard."""
    assert result.status.value == "error"
    assert not psutil.pid_exists(process.pid), (
        f"child pid {process.pid} still exists after a selector "
        f"registration failure -- the failure occurred before the "
        f"cleanup guard was armed, leaking a live child process"
    )
    assert process.stdout is not None and process.stdout.closed, (
        "process.stdout was never closed after a selector registration "
        "failure -- the failure occurred before the cleanup guard was armed"
    )
    assert process.stderr is not None and process.stderr.closed, (
        "process.stderr was never closed after a selector registration "
        "failure -- the failure occurred before the cleanup guard was armed"
    )


class TestSelectorSetupCoveredByCleanupGuard:
    """Priority 3: selector creation/registration must be covered by the
    same terminate-and-close cleanup guarantee as the rest of the
    supervision loop."""

    @pytest.mark.asyncio
    async def test_register_failure_still_terminates_and_reaps_real_child(
        self, tmp_path
    ):
        output_path = str(tmp_path / "output.txt")
        captured: dict = {}
        executor = SubprocessExecutor(max_workers=1)
        try:
            with (
                patch(
                    "code_indexer.server.services.subprocess_executor.subprocess.Popen",
                    side_effect=_capture_popen_factory(captured),
                ),
                patch(
                    "code_indexer.server.services.subprocess_executor.selectors.DefaultSelector",
                    side_effect=_capture_selector_factory(captured),
                ),
            ):
                result = await executor.execute_with_limits(
                    command=[sys.executable, "-c", _SLEEPY_SCRIPT],
                    working_dir=str(tmp_path),
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                    output_file_path=output_path,
                    max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
                )

            assert "process" in captured, "the real subprocess was never spawned"
            _assert_child_terminated_and_streams_closed(result, captured["process"])
        finally:
            # Safety net covering BOTH failure modes: execute_with_limits
            # raising outright, or the assertions above failing. Never
            # leave a real child process or a real open epoll fd past
            # this test either way.
            _cleanup_leaked_process(captured.get("process"))
            _cleanup_leaked_selector(captured.get("selector"))
            executor.shutdown(wait=True)
