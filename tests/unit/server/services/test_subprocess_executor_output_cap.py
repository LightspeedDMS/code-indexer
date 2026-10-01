"""Unit tests for SubprocessExecutor's output-size-capped early termination.

Issue #1601 (Fix direction 4a/4b): SubprocessExecutor.execute_with_limits()
historically blocked on process.communicate(timeout=...) until the subprocess
fully exited before the caller regained control -- by then, an unbounded
writer may already have produced gigabytes of output. These tests prove the
new max_output_bytes parameter terminates the subprocess WHILE it is still
producing output, the moment its output file crosses the ceiling, rather than
merely bounding a later read of an already-finished file.

Fast tier (AC-A6): no long sleeps -- the test subprocess writes quickly and
the ceiling is small, so detection + kill happens in tens of milliseconds.

Remediation round (code review REJECT, both Claude and Codex): the original
poll-only implementation had two compounding defects, both covered here:

- Priority 1a: it never drained the child's stderr pipe while polling the
  output FILE's size, so a child that blocked inside a stderr write()
  syscall (pipe buffer full, nobody reading) looked identical to one still
  legitimately running -- the poll loop spun until the full wall-clock
  deadline instead of completing promptly.
- Priority 1b: the byte ceiling was enforced by periodically checking the
  output file's on-disk size rather than bounding the WRITE itself, so a
  fast writer could produce up to several multiples of the configured
  ceiling before the next poll caught it.
"""

import os
import time

import pytest

from code_indexer.server.services.subprocess_executor import (
    _READ_CHUNK_BYTES,
    ExecutionStatus,
    SubprocessExecutor,
)

# Test-subprocess tuning: writes far more than the ceiling below, slowly
# enough (a short sleep between chunks) that the executor's polling loop can
# observe and kill it mid-stream instead of racing it to natural completion.
_WRITER_CHUNK_BYTES = 4096
_WRITER_ITERATIONS = 500
_WRITER_SLEEP_SECONDS = 0.01
_WRITER_TOTAL_BYTES = _WRITER_ITERATIONS * _WRITER_CHUNK_BYTES

_SLOW_WRITER_SCRIPT = (
    "import sys, time\n"
    f"for _ in range({_WRITER_ITERATIONS}):\n"
    f"    sys.stdout.write('x' * {_WRITER_CHUNK_BYTES})\n"
    "    sys.stdout.flush()\n"
    f"    time.sleep({_WRITER_SLEEP_SECONDS})\n"
)

# Test ceiling. Priority 1b: the redesigned executor bounds a single stdout
# read to at most _READ_CHUNK_BYTES, so the real worst-case overshoot above
# max_output_bytes is bounded by ONE chunk read -- not an arbitrary
# multiplier tolerant of coarse poll-interval slack.
_TEST_MAX_OUTPUT_BYTES = 8192
_EXECUTION_TIMEOUT_SECONDS = 30

# Priority 1a regression: a child that floods stderr well beyond the OS
# pipe buffer (commonly 64 KiB on Linux) while writing very little to
# stdout. Empirically reproduced against the pre-remediation code: the
# child blocks forever inside its stderr write() syscall (nobody reading
# the pipe), so process.poll() never returns and the old poll-only
# implementation spun until the full wall-clock deadline.
_STDERR_FLOOD_BYTES = 300_000
_STDERR_FLOOD_SCRIPT = (
    "import sys\n"
    "sys.stdout.write('ok\\n')\n"
    "sys.stdout.flush()\n"
    f"sys.stderr.write('e' * {_STDERR_FLOOD_BYTES})\n"
    "sys.stderr.flush()\n"
)
_DEADLOCK_TEST_TIMEOUT_SECONDS = 8


class TestSubprocessExecutorOutputCap:
    """max_output_bytes terminates a still-running subprocess early."""

    @pytest.mark.asyncio
    async def test_terminates_subprocess_when_output_cap_exceeded_before_natural_completion(
        self, tmp_path
    ):
        """Killed while running, not merely read-capped after completion."""
        output_path = str(tmp_path / "capped_output.txt")
        executor = SubprocessExecutor(max_workers=1)
        try:
            result = await executor.execute_with_limits(
                command=["python3", "-c", _SLOW_WRITER_SCRIPT],
                working_dir=str(tmp_path),
                timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                output_file_path=output_path,
                max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
            )
        finally:
            executor.shutdown(wait=True)

        # The new, distinct signal: this call was capped by the byte ceiling,
        # not by the wall-clock timeout, and not a normal clean exit.
        assert result.output_capped is True
        assert result.timed_out is False
        # A killed-due-to-output-cap process is a deliberate, EXPECTED
        # outcome (partial results are legitimate), never an ERROR status
        # -- callers must be able to proceed to read the (bounded) partial
        # output rather than treat this as a search failure.
        assert result.status == ExecutionStatus.SUCCESS

        # Proof of actual process termination (not merely "reading stopped"):
        # a process killed via Popen.kill() (SIGKILL) reports a NEGATIVE
        # returncode on POSIX (the negated signal number, e.g. -9) -- never
        # the natural 0 the script would exit with if left to finish all
        # _WRITER_ITERATIONS iterations undisturbed, and never a positive
        # application-error code either.
        assert result.exit_code is not None
        assert result.exit_code < 0

        # The output file must be bounded far below what the script would
        # have produced had it been allowed to run to completion -- proof
        # the WRITE itself was interrupted, not just the caller's read.
        written_bytes = os.path.getsize(output_path)
        assert written_bytes < _WRITER_TOTAL_BYTES
        # Priority 1b (tightened): the write itself is bounded by a single
        # chunk read past the ceiling, not a coarse multiplier -- proof the
        # cap is enforced at write-time, not merely observed on a poll tick.
        assert written_bytes < _TEST_MAX_OUTPUT_BYTES + _READ_CHUNK_BYTES

    @pytest.mark.asyncio
    async def test_output_capped_false_when_process_completes_under_the_ceiling(
        self, tmp_path
    ):
        """Regression lock: a call that finishes naturally, comfortably under
        max_output_bytes, must report output_capped=False and a clean exit
        -- the new parameter must not affect a normal small-output call."""
        output_path = str(tmp_path / "small_output.txt")
        executor = SubprocessExecutor(max_workers=1)
        try:
            result = await executor.execute_with_limits(
                command=["echo", "small output"],
                working_dir=str(tmp_path),
                timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                output_file_path=output_path,
                max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
            )
        finally:
            executor.shutdown(wait=True)

        assert result.output_capped is False
        assert result.timed_out is False
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_stderr_flood_does_not_deadlock_the_capped_wait(self, tmp_path):
        """Priority 1a: a child that floods stderr beyond the OS pipe
        buffer, while its stdout stays comfortably under the byte ceiling,
        must complete promptly -- not hang until the full wall-clock
        timeout. Proves the executor concurrently drains stderr while
        supervising the byte-capped stdout, so the child can never block
        on a full stderr pipe."""
        output_path = str(tmp_path / "stderr_flood_output.txt")
        executor = SubprocessExecutor(max_workers=1)
        try:
            start = time.monotonic()
            result = await executor.execute_with_limits(
                command=["python3", "-c", _STDERR_FLOOD_SCRIPT],
                working_dir=str(tmp_path),
                timeout_seconds=_DEADLOCK_TEST_TIMEOUT_SECONDS,
                output_file_path=output_path,
                max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
            )
            elapsed = time.monotonic() - start
        finally:
            executor.shutdown(wait=True)

        assert result.timed_out is False, (
            f"call hung until (or near) the wall-clock timeout "
            f"({elapsed:.1f}s) instead of completing once the child's "
            f"stderr flood was drained and it exited"
        )
        assert result.output_capped is False
        assert result.exit_code == 0
        assert elapsed < 3.0, (
            f"expected prompt completion once stderr is drained "
            f"concurrently with the stdout cap check, took {elapsed:.1f}s"
        )
