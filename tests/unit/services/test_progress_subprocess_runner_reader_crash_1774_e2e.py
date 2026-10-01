"""
Regression tests for GitHub Bug #1774 (indexing lockup) -- part 3 of 3
(end-to-end reproduction through the public entry point). See
test_progress_subprocess_runner_reader_crash_1774_root_cause.py for the
full bug/fix narrative and findings A-D.

This is finding D's genuinely discriminating replacement for the
original, merely-structural RED evidence (an `ImportError` proving the
extraction happened, not that the original nested-closure bug would
have failed for the right reason): it pads the process's real fd table
past 1024 with real, currently-open fds (mirroring how a long-lived
server process accumulates fds over its lifetime -- the actual
production root cause), so the REAL Popen-created stdout/stderr pipes
for a child spawned through the PUBLIC `run_with_popen_progress()`
genuinely land above glibc's FD_SETSIZE=1024 ceiling, then runs a child
that writes > 64KB of stdout (larger than the default OS pipe buffer --
an undrained reader would let the child block forever writing it,
reproducing the actual production lockup) and asserts the call returns
promptly with the full payload captured.

Empirically verified while writing this test: elapsed ~0.04s, clean
return, full 70000-byte payload captured, with the fd table padded to
put the child's pipes at fd ~1100+.

Real OS primitives only -- real fds, real subprocess, no mocking, no
monkeypatching. A thread-based watchdog wraps the call so a regression
back to the pre-fix hang fails this test loudly (via `pytest.fail`)
instead of hanging the whole suite; a unique marker embedded in the
child script lets a `finally` unconditionally `pkill` any leaked child
(and, transitively, unblock the leaked daemon thread) even when the
watchdog itself times out.
"""

import os
import subprocess
import sys
import threading
import time
import uuid
from typing import List

import pytest

_SUITE_TIMEOUT_SECONDS = 30
pytestmark = pytest.mark.timeout(_SUITE_TIMEOUT_SECONDS)

_PAD_FD_COUNT = 1100
_LARGE_OUTPUT_BYTES = 70_000
_END_TO_END_TEST_BOUND_SECONDS = 15.0


def _close_all_quietly(fds: List[int]) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _pad_fd_table(count: int) -> List[int]:
    """Open `count` real, currently-open fds via /dev/null -- mirrors how
    a long-lived server process accumulates fds over its lifetime, the
    actual Bug #1774 production precondition.

    Builds the list incrementally (not a bare list comprehension) so
    that if `os.open()` fails partway through, every fd already opened
    is closed before the failure propagates -- nothing is lost to the
    caller.
    """
    fds: List[int] = []
    try:
        for _ in range(count):
            fds.append(os.open(os.devnull, os.O_RDONLY))
    except OSError:
        _close_all_quietly(fds)
        raise
    return fds


def _build_large_stdout_child_command(marker: str) -> List[str]:
    """A real child writing a payload bigger than the default OS pipe
    buffer. `marker` is a dead comment purely so a leaked process (only
    possible if this test's watchdog itself times out) can be found and
    force-killed by `pkill -f` in cleanup.
    """
    child_script = (
        f"# {marker}\n"
        "import sys\n"
        f"sys.stdout.write('x' * {_LARGE_OUTPUT_BYTES})\n"
        "sys.stdout.flush()\n"
    )
    return [sys.executable, "-c", child_script]


def _run_with_watchdog(fn, bound_seconds: float) -> tuple:
    """Run `fn()` on a background daemon thread, joined with a bounded
    timeout. Returns (worker, elapsed, result_holder). The worker never
    blocks process exit even if still alive when this returns (the
    regression case) -- explicit cleanup is the caller's job.
    """
    result_holder: dict = {}

    def run_call() -> None:
        try:
            fn()
            result_holder["outcome"] = "returned"
        except Exception as exc:  # noqa: BLE001 - capturing for assertions
            result_holder["outcome"] = "raised"
            result_holder["exception"] = exc

    start = time.monotonic()
    worker = threading.Thread(target=run_call, daemon=True)
    worker.start()
    worker.join(timeout=bound_seconds)
    elapsed = time.monotonic() - start
    return worker, elapsed, result_holder


def _assert_end_to_end_success(
    worker: threading.Thread,
    elapsed: float,
    result_holder: dict,
    all_stdout: List[str],
) -> None:
    if worker.is_alive():
        pytest.fail(
            f"run_with_popen_progress did not return within "
            f"{_END_TO_END_TEST_BOUND_SECONDS}s with a padded fd table "
            f"-- this is the actual Bug #1774 production lockup"
        )
    assert result_holder.get("outcome") == "returned", (
        f"expected a clean return, got: {result_holder}"
    )
    assert elapsed < _END_TO_END_TEST_BOUND_SECONDS
    captured = sum(len(line) for line in all_stdout)
    # Exact match, not a tolerant threshold (Codex round-2 finding): a
    # broken reader that silently drops part of the output must not be
    # able to pass this test by only losing a fraction of it.
    assert captured == _LARGE_OUTPUT_BYTES, (
        f"expected the FULL large stdout payload to be captured (the "
        f"reader must have actually drained all of it, not just most "
        f"of it); captured {captured} of "
        f"{_LARGE_OUTPUT_BYTES} bytes"
    )


def _pkill_marker_best_effort(marker: str) -> None:
    """Best-effort cleanup: only ever finds a live process if the
    watchdog timed out (worker still alive); a properly returning run
    leaves no such child. Tolerates `pkill` being unavailable on some
    future CI environment -- a cleanup failure must never mask the
    actual test result raised/asserted above it in the `finally`.
    """
    try:
        subprocess.run(["pkill", "-f", marker], check=False)
    except OSError:
        pass


class TestRunWithPopenProgressEndToEndSurvivesHighFdTable:
    """See module docstring for the full rationale."""

    def test_high_fd_table_does_not_hang_public_entry_point(self):
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        marker = f"bug1774_e2e_{uuid.uuid4().hex}"
        padding_fds = _pad_fd_table(_PAD_FD_COUNT)
        try:
            allocator = ProgressPhaseAllocator()
            allocator.calculate_weights(
                index_types=["semantic"], file_count=1, commit_count=0
            )
            command = _build_large_stdout_child_command(marker)
            all_stdout: List[str] = []

            def make_call() -> None:
                run_with_popen_progress(
                    command=command,
                    phase_name="semantic",
                    allocator=allocator,
                    progress_callback=None,
                    all_stdout=all_stdout,
                    all_stderr=[],
                    cwd=None,
                )

            worker, elapsed, result_holder = _run_with_watchdog(
                make_call, _END_TO_END_TEST_BOUND_SECONDS
            )
            _assert_end_to_end_success(worker, elapsed, result_holder, all_stdout)
        finally:
            _close_all_quietly(padding_fds)
            _pkill_marker_best_effort(marker)


# --- Round 2 regressions: finally-block fd-close-ordering defect ----------
#
# Both Codex and Claude (independent round-2 reviews) converged on ONE root
# cause manifesting as two symptoms: the shutdown pipe fds were being closed
# BEFORE stderr_thread was guaranteed to have joined (stderr_thread was
# previously joined only long after this finally block, by which point the
# fds were already closed -- or, on the exception exit path, never joined
# here at all). The fix (see progress_subprocess_runner.py's finally block)
# joins BOTH reader threads before closing either shutdown fd. The two test
# classes below reproduce each reviewer's finding directly.

_GRANDCHILD_STDERR_RACE_ITERATIONS = 20
# Deliberately wide margin (matches TestC1StderrGrandchildHang's own
# precedent in test_progress_subprocess_runner.py): a call that
# incorrectly waited for any meaningful fraction of the grandchild's
# lifetime must fail this bound loudly, not pass by coincidence.
_GRANDCHILD_STDERR_RACE_SLEEP_SECONDS = 8
_GRANDCHILD_STDERR_RACE_BOUND_SECONDS = 4.0


def _build_grandchild_holds_stderr_command() -> List[str]:
    """The exact scenario both round-2 reviewers used: child prints one
    stdout line (fast EOF -- this is the natural-stdout-EOF break path,
    the one the old code's finally block mishandled), spawns a grandchild
    that inherits ONLY stderr (`close_fds=False` with stdin/stdout
    explicitly redirected to DEVNULL -- Claude round-3 finding: without
    the redirect, the grandchild also inherited the child's stdout pipe,
    so stdout never reached genuine EOF and the test always took the
    process.poll()-detected-exit branch instead of the natural-EOF
    branch it claims to guard) and sleeps, then exits immediately. This
    is literally the scenario the shutdown-pipe mechanism exists to
    handle: the call must not wait for the grandchild, and (post-fix)
    must not falsely flag the stderr reader as having failed just
    because it hadn't yet observed the shutdown byte when the old code
    raced to close the fds.
    """
    script = (
        "import json, subprocess, sys\n"
        'print(json.dumps({"current": 1, "total": 1, "info": "done"}), '
        "flush=True)\n"
        "subprocess.Popen(['sleep', "
        f"'{_GRANDCHILD_STDERR_RACE_SLEEP_SECONDS}'], close_fds=False, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)\n"
        "sys.exit(0)\n"
    )
    return [sys.executable, "-c", script]


class TestStderrGrandchildRaceNeverFalselyFlagsReaderFailed:
    """HIGH-1 (Claude, reproduced 15/15 pre-fix): on the natural-stdout-EOF
    path, the stderr reader thread was often still alive (blocked on its
    own select(), registered on shutdown_r) at the exact moment the old
    finally block closed shutdown_r/shutdown_w -- without ever having
    joined it first. The reader would then hit the fd-closed staleness
    probe and get falsely flagged `reader_failed`, logging a false
    "reader thread failed unexpectedly" WARNING on every healthy run of
    this exact scenario (which is not cosmetic: this log line is not in
    `LOG_AUDIT_ALLOWLIST`, so it would fail the Post-E2E log-audit gate).

    RED (pre-fix, this round): reproducible ~15/15 per the reviewer's
    independent finding.

    GREEN (post-fix): stderr_thread is now joined (right after the
    shutdown byte is written) BEFORE either fd is closed, so the reader
    always observes the byte as a normal, valid readiness event -- never
    a "closed without being observed" staleness condition. Repeated here
    for statistical confidence.
    """

    def test_no_false_positive_reader_failed_across_repeated_runs(self, caplog):
        import logging

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        command = _build_grandchild_holds_stderr_command()

        with caplog.at_level(logging.WARNING):
            for iteration in range(_GRANDCHILD_STDERR_RACE_ITERATIONS):
                allocator = ProgressPhaseAllocator()
                allocator.calculate_weights(
                    index_types=["semantic"], file_count=10, commit_count=0
                )
                all_stdout: List[str] = []
                all_stderr: List[str] = []

                start = time.monotonic()
                run_with_popen_progress(
                    command=command,
                    phase_name="semantic",
                    allocator=allocator,
                    progress_callback=None,
                    all_stdout=all_stdout,
                    all_stderr=all_stderr,
                    cwd=None,
                )
                elapsed = time.monotonic() - start

                assert elapsed < _GRANDCHILD_STDERR_RACE_BOUND_SECONDS, (
                    f"iteration {iteration}: took {elapsed:.2f}s -- must "
                    f"not wait for the grandchild's "
                    f"{_GRANDCHILD_STDERR_RACE_SLEEP_SECONDS}s sleep"
                )

        assert "reader thread failed unexpectedly" not in caplog.text, (
            f"false-positive abnormal-reader-termination log detected "
            f"across {_GRANDCHILD_STDERR_RACE_ITERATIONS} healthy runs: "
            f"{caplog.text!r}"
        )
        assert "terminated abnormally" not in caplog.text, (
            f"false-positive summary WARNING detected across "
            f"{_GRANDCHILD_STDERR_RACE_ITERATIONS} healthy runs: "
            f"{caplog.text!r}"
        )
        assert "did not join within" not in caplog.text, (
            f"a reader thread join unexpectedly timed out across "
            f"{_GRANDCHILD_STDERR_RACE_ITERATIONS} healthy runs: "
            f"{caplog.text!r}"
        )


class TestReaderThreadsAreFullyReapedNotLeakedHigh2:
    """HIGH-2 (Claude 12/12, Codex 100/100, both reproduced independently):
    `_fd_is_open()`'s `os.fstat(fd)` check answers "is this fd NUMBER
    open," not "is this the same open file DESCRIPTION my reader was
    watching." Once the old code closed shutdown_r/shutdown_w while the
    stderr reader might still be alive, and the OS recycled those fd
    numbers for something unrelated (routine in a busy process), the
    staleness probe would report "still open" for a completely different
    file description -- so it never fired, and the reader spun forever:
    a leaked daemon thread waking on every poll interval, permanently,
    per invocation.

    The real fix eliminates the race window this depends on entirely
    (fds are only closed once both reader threads are CONFIRMED joined),
    so fd-number reuse while a reader might still reference the number
    can no longer happen. The reliable, honest way to verify this
    black-box is to confirm the OBSERVABLE CONSEQUENCE: no thread
    spawned during the call is still alive once the call returns. Daemon
    threads that are genuinely done are removed from
    `threading.enumerate()` automatically; a leaked/spinning reader
    thread (HIGH-2's actual symptom) would still be alive and thus still
    present in the diff.

    Claude round-3 finding: the `threading.enumerate()` check alone
    would NOT have caught the round-2 defect even on the correct code
    path -- a falsely-flagged stderr reader (HIGH-1's symptom) still
    returns/exits promptly after being flagged rather than actually
    leaking a spinning thread, so it would pass this thread-leak check
    even while silently mislabeling a healthy run. The discriminating
    assertion is the log/raise check below, matching
    `TestStderrGrandchildRaceNeverFalselyFlagsReaderFailed`'s pattern.
    """

    def test_no_reader_thread_survives_the_call(self, caplog):
        import logging

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"], file_count=10, commit_count=0
        )
        command = _build_grandchild_holds_stderr_command()

        threads_before = set(threading.enumerate())
        with caplog.at_level(logging.WARNING):
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=None,
                all_stdout=[],
                all_stderr=[],
                cwd=None,
            )
        threads_after = set(threading.enumerate())

        leaked = threads_after - threads_before
        assert not leaked, (
            f"thread(s) still alive after run_with_popen_progress "
            f"returned -- this is the HIGH-2 leaked-reader-thread "
            f"symptom: {leaked}"
        )
        assert "reader thread failed unexpectedly" not in caplog.text, (
            f"false-positive abnormal-reader-termination log detected: {caplog.text!r}"
        )
        assert "terminated abnormally" not in caplog.text, (
            f"false-positive summary WARNING detected: {caplog.text!r}"
        )
        assert "did not join within" not in caplog.text, (
            f"a reader thread join unexpectedly timed out: {caplog.text!r}"
        )
