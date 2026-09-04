"""
Regression tests for GitHub Bug #1774 (indexing lockup) -- part 1 of 3
(environmental/stdlib root-cause evidence). See also:
  - test_progress_subprocess_runner_reader_crash_1774_reader_loops.py
  - test_progress_subprocess_runner_reader_crash_1774_e2e.py
Split across three files per code review feedback on module size; shared
fd/pipe helpers live in bug1774_reader_crash_helpers.py.

Root cause (full write-up in issue #1774; mechanism also documented in
`progress_subprocess_runner.py`'s own Bug #1774 comments -- not repeated
here): the old nested `_stdout_reader`/`_stderr_reader` closures called
`select.select()`, which raises `ValueError` (NOT `OSError`) for any
monitored fd >= 1024. That escaped the narrow `except OSError` handler,
silently killing the reader thread before it ever pushed its completion
sentinel. The main loop still noticed the thread was no longer alive and
moved on to an unconditional `process.wait()` -- which then hung
forever, because the child's own writer threads were still blocked
writing to a stdout pipe nobody was draining anymore.

Fix (see `progress_subprocess_runner.py`; deliberately scoped to NOT add
a force-kill/bounded-wait mechanism -- that was considered and dropped
as scope creep):
  1. Both reader loops now catch `Exception` (not just `OSError`) and log
     with a full traceback, guaranteeing the sentinel/flush always runs.
  2. Both reader loops now use `selectors.DefaultSelector()` (epoll,
     no FD_SETSIZE ceiling) instead of `select.select()`.

Two independent code reviews (Codex + Claude) on the first pass of this
fix found four more real issues, each covered in the reader_loops/e2e
files:
  A. `sel.select()` had no timeout; epoll silently drops a closed fd
     instead of raising like select.select() did, so it could block
     forever if a fd was closed mid-wait.
  B. A genuine reader failure was silently indistinguishable from clean
     EOF.
  C. Caller-side chunk processing sat outside the generator's
     try/except, so a failure there wasn't caught.
  D. The original RED evidence was structural (ImportError), not
     behavioral.

None of this changes Bug #1218: `process.wait()` remains completely
unconditional and unbounded for a healthy job. The `reader_failed` flag
(finding B) DOES now surface as a real, distinct raised
`IndexingSubprocessError` (see `_raise_if_reader_failed`) rather than
only a log line -- but this is a synchronous decision based on a flag
that is already known by the time it is checked (no additional waiting
or polling), so it still does not reintroduce a wall-clock timeout on
the job or subprocess.

This file: not a test of our code -- factual, evidence-first
confirmation (Messi Rule #10) of the two stdlib behaviors the fix
depends on. Real OS primitives only (real pipes, real duplicated fds via
`fcntl.F_DUPFD` -- never `os.dup2` onto a hardcoded number, which could
silently clobber an unrelated already-open descriptor).
"""

import os
import select as select_module
import selectors

import pytest

from tests.unit.services.bug1774_reader_crash_helpers import (
    SELECTOR_WAIT_TIMEOUT_SECONDS,
    high_fd_pipe,
)

_SUITE_TIMEOUT_SECONDS = 30
pytestmark = pytest.mark.timeout(_SUITE_TIMEOUT_SECONDS)


class TestSelectSelectRootCauseEnvironmentalPrecondition:
    """Confirms this system's `select.select()` really does raise
    `ValueError` (not `OSError`) for a fd >= 1024 -- the ground truth
    this fix is built on. If this test itself ever fails, the whole bug
    analysis needs to be revisited.
    """

    def test_select_select_raises_valueerror_not_oserror_for_high_fd(self):
        with high_fd_pipe() as (high_fd, _write_fd):
            with pytest.raises(ValueError) as exc_info:
                select_module.select([high_fd], [], [], 0)
            assert not isinstance(exc_info.value, OSError), (
                "must NOT be an OSError subclass, or the original bug "
                "could never have happened"
            )


class TestSelectorsAvailableForDefenseInDepth:
    """Stdlib sanity check: `selectors.DefaultSelector` has no practical
    fd ceiling here, confirming the fix's chosen mechanism actually
    closes the vulnerability.
    """

    def test_default_selector_registers_high_fd_without_error(self):
        with high_fd_pipe() as (high_fd, write_fd):
            with selectors.DefaultSelector() as sel:
                sel.register(high_fd, selectors.EVENT_READ)
                os.write(write_fd, b"x")
                ready = sel.select(timeout=SELECTOR_WAIT_TIMEOUT_SECONDS)
                assert ready, "selector never reported the high fd as ready"
