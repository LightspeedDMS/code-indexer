"""Issue #1601 remediation round 4, Priority 1 (REQUIRED, both reviewers
independently found this; Claude's reviewer empirically reproduced it with
a real harness under ``-W error::ResourceWarning``).

``_wait_with_output_cap``'s ``finally`` block only ever called ``sel.close()``
-- it never closed ``process.stdout``/``process.stderr``. Every regex search
that goes through the output-capped path (i.e. every call, since
``max_output_bytes`` is always supplied by ``_search_ripgrep``/
``_search_grep``) leaks two open pipe file descriptors to GC finalization,
on EVERY code path through this function -- capped, natural-EOF, and
timeout alike. At concurrent production load against a default 1024 fd
ulimit this is a real resource-exhaustion risk.

Separately: if ``open(output_file_path, "wb")`` itself raises (e.g. the
path is unwritable) or a write mid-loop raises, the exception used to
escape straight to the caller with the child NEVER terminated and NEVER
reaped -- a real hole in the "guaranteed reaping on all paths" claim from
an earlier remediation round.

These tests prove, with a REAL child process (``subprocess.Popen`` wrapped
only to observationally capture the real Popen object, exactly like
``test_subprocess_executor_reaping_1601.py``'s established pattern):

1. On the (already-covered) capped-output path, both ``process.stdout`` and
   ``process.stderr`` are closed by the time ``execute_with_limits``
   returns, and no ``ResourceWarning`` is raised.
2. On a forced supervision failure (the output file path is unwritable,
   so ``open(output_file_path, "wb")`` raises inside the try block), the
   real child is terminated and fully reaped (no zombie), both streams are
   closed, and no ``ResourceWarning`` is raised.
"""

from __future__ import annotations

import gc
import subprocess
import warnings
from unittest.mock import patch

import psutil
import pytest

from code_indexer.server.services.subprocess_executor import SubprocessExecutor

_TEST_MAX_OUTPUT_BYTES = 8192
_EXECUTION_TIMEOUT_SECONDS = 30

# Writes far more than the ceiling, slowly enough that the executor's
# selector loop observes and kills it mid-stream rather than racing it to
# natural completion -- mirrors test_subprocess_executor_output_cap.py's
# established slow-writer pattern.
_WRITER_CHUNK_BYTES = 4096
_WRITER_ITERATIONS = 500
_WRITER_SLEEP_SECONDS = 0.01
_SLOW_WRITER_SCRIPT = (
    "import sys, time\n"
    f"for _ in range({_WRITER_ITERATIONS}):\n"
    f"    sys.stdout.write('x' * {_WRITER_CHUNK_BYTES})\n"
    "    sys.stdout.flush()\n"
    f"    time.sleep({_WRITER_SLEEP_SECONDS})\n"
)

# A real child that survives long enough to prove active termination (not
# a natural exit) forced the reaping -- bounded, finite sleep.
_SLEEPY_SCRIPT = "import time\ntime.sleep(5)\n"


def _capture_popen_factory(captured: dict):
    real_popen = subprocess.Popen

    def _capture_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured["process"] = proc
        return proc

    return _capture_popen


class TestSubprocessExecutorFdLeak:
    """Priority 1: no leaked stdout/stderr FDs on any path through
    ``_wait_with_output_cap``, including the exception path."""

    @pytest.mark.asyncio
    @pytest.mark.filterwarnings("error::ResourceWarning")
    async def test_capped_path_closes_streams_and_raises_no_resource_warning(
        self, tmp_path
    ):
        output_path = str(tmp_path / "capped_output.txt")
        captured: dict = {}
        executor = SubprocessExecutor(max_workers=1)
        try:
            with patch(
                "code_indexer.server.services.subprocess_executor.subprocess.Popen",
                side_effect=_capture_popen_factory(captured),
            ):
                result = await executor.execute_with_limits(
                    command=["python3", "-c", _SLOW_WRITER_SCRIPT],
                    working_dir=str(tmp_path),
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                    output_file_path=output_path,
                    max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
                )
        finally:
            executor.shutdown(wait=True)

        assert result.output_capped is True
        assert "process" in captured, "the real subprocess was never spawned"
        process = captured["process"]

        # Force any pending GC-finalization-triggered ResourceWarning to
        # surface deterministically within this test's filterwarnings scope
        # (a real leaked FileIO's __del__ would raise it here as an error).
        gc.collect()

        assert process.stdout is not None and process.stdout.closed, (
            "process.stdout was never closed -- leaked pipe FD"
        )
        assert process.stderr is not None and process.stderr.closed, (
            "process.stderr was never closed -- leaked pipe FD"
        )

    @pytest.mark.asyncio
    @pytest.mark.filterwarnings("error::ResourceWarning")
    async def test_supervision_failure_terminates_reaps_and_closes_streams(
        self, tmp_path
    ):
        # A directory, not a file: open(path, "wb") raises IsADirectoryError
        # immediately, inside the try block wrapping the selector loop --
        # forcing the exception path without needing to inject a mid-loop
        # write failure.
        unwritable_output_path = tmp_path / "not_a_file"
        unwritable_output_path.mkdir()

        captured: dict = {}
        executor = SubprocessExecutor(max_workers=1)
        try:
            with patch(
                "code_indexer.server.services.subprocess_executor.subprocess.Popen",
                side_effect=_capture_popen_factory(captured),
            ):
                result = await executor.execute_with_limits(
                    command=["python3", "-c", _SLEEPY_SCRIPT],
                    working_dir=str(tmp_path),
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                    output_file_path=str(unwritable_output_path),
                    max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
                )
        finally:
            executor.shutdown(wait=True)

        assert result.status.value == "error"
        assert "process" in captured, "the real subprocess was never spawned"
        process = captured["process"]

        gc.collect()

        # Real reaping: the child must be terminated, not left running or
        # zombified, even though supervision itself blew up.
        assert not psutil.pid_exists(process.pid), (
            f"child pid {process.pid} still exists after a supervision "
            f"failure -- not terminated/reaped"
        )
        assert process.stdout is not None and process.stdout.closed, (
            "process.stdout was never closed on the supervision-failure path"
        )
        assert process.stderr is not None and process.stderr.closed, (
            "process.stderr was never closed on the supervision-failure path"
        )

    @pytest.mark.asyncio
    async def test_no_resource_warning_flood_under_explicit_warning_capture(
        self, tmp_path
    ):
        """Belt-and-suspenders: reproduces the exact reviewer harness shape
        (explicit warnings.catch_warnings + gc.collect()) independent of
        pytest's own filterwarnings machinery."""
        output_path = str(tmp_path / "capped_output2.txt")
        executor = SubprocessExecutor(max_workers=1)
        try:
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                result = await executor.execute_with_limits(
                    command=["python3", "-c", _SLOW_WRITER_SCRIPT],
                    working_dir=str(tmp_path),
                    timeout_seconds=_EXECUTION_TIMEOUT_SECONDS,
                    output_file_path=output_path,
                    max_output_bytes=_TEST_MAX_OUTPUT_BYTES,
                )
                gc.collect()
        finally:
            executor.shutdown(wait=True)

        assert result.output_capped is True
        resource_warnings = [
            w for w in recorded if issubclass(w.category, ResourceWarning)
        ]
        assert not resource_warnings, (
            f"unexpected ResourceWarning(s) raised: "
            f"{[str(w.message) for w in resource_warnings]}"
        )
