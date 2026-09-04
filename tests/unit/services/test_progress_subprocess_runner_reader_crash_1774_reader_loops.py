"""
Regression tests for GitHub Bug #1774 (indexing lockup) -- part 2 of 3
(unit-level reader-loop tests). See
test_progress_subprocess_runner_reader_crash_1774_root_cause.py for the
full bug/fix narrative and findings A-D; shared fd/pipe helpers live in
bug1774_reader_crash_helpers.py.

Real OS primitives only (real pipes, real fds) -- no mocking of the SUT,
no monkeypatching of any stdlib global or internal. The one fake
collaborator used (`_FlakyQueueWrapper`, finding C) stands in for a
caller-supplied dependency (`line_queue`), never the SUT itself.
"""

import logging
import os
import queue as queue_module
import threading
import time

import pytest

from tests.unit.services.bug1774_reader_crash_helpers import (
    CLOSED_FD_RACE_SETTLE_SECONDS,
    THREAD_JOIN_TIMEOUT_SECONDS,
    close_quietly,
    drain_queue,
    high_fd_pipe,
    high_unused_fd,
    run_stderr_reader_loop,
    run_stdout_reader_loop,
)

_SUITE_TIMEOUT_SECONDS = 60
pytestmark = pytest.mark.timeout(_SUITE_TIMEOUT_SECONDS)


class TestStdoutReaderLoopSurvivesHighFd:
    """Fix part 2 (selectors): a real fd >= 1024 is handled like any
    other fd -- no exception, sentinel arrives, reader_failed stays
    clear.

    RED (pre-fix): `_stdout_reader_loop` doesn't exist as a module-level
    function -- ImportError.
    """

    def test_stdout_reader_loop_survives_high_fd_and_pushes_sentinel(self):
        with high_fd_pipe() as (high_fd, write_fd):
            thread, line_queue, reader_failed, shutdown_r, shutdown_w = (
                run_stdout_reader_loop(high_fd)
            )
            try:
                payload = b"hello from high fd\n"
                assert os.write(write_fd, payload) == len(payload), (
                    "expected a full, non-partial write to the pipe"
                )
                close_quietly(write_fd)  # natural EOF

                thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
                assert not thread.is_alive(), (
                    f"reader loop thread did not finish within "
                    f"{THREAD_JOIN_TIMEOUT_SECONDS}s -- hung on the high "
                    f"fd instead of handling it via selectors"
                )
                assert reader_failed.is_set() is False, (
                    "a clean high-fd EOF must never be flagged as abnormal"
                )
                received = drain_queue(line_queue)
                assert "hello from high fd\n" in received
                assert received[-1] is None, "sentinel must always be pushed"
            finally:
                close_quietly(shutdown_r)
                close_quietly(shutdown_w)


class TestStdoutReaderLoopCatchesGenuineFailureAndStillPushesSentinel:
    """Fix part 1 (widened exception handling): a genuinely bad fd (a
    real OS-level EBADF, not a mock) is caught, logged with a full
    traceback (caplog), flagged via `reader_failed`, and the sentinel
    still arrives.

    RED (pre-fix): ImportError, same reason as above.
    """

    def test_bad_fd_is_caught_logged_flagged_and_sentinel_still_arrives(self, caplog):
        bad_fd = high_unused_fd()  # closed before return -- genuinely invalid

        with caplog.at_level(logging.ERROR):
            thread, line_queue, reader_failed, shutdown_r, shutdown_w = (
                run_stdout_reader_loop(bad_fd)
            )
            try:
                thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
                assert not thread.is_alive(), (
                    f"reader loop thread did not finish within "
                    f"{THREAD_JOIN_TIMEOUT_SECONDS}s against a bad fd"
                )
                assert reader_failed.is_set(), (
                    "a genuine internal failure must be flagged via "
                    "reader_failed -- finding B"
                )
                assert line_queue.get_nowait() is None, (
                    "sentinel must still be pushed after a genuine failure"
                )
            finally:
                close_quietly(shutdown_r)
                close_quietly(shutdown_w)

        assert "reader thread failed unexpectedly" in caplog.text
        assert any(record.exc_info for record in caplog.records), (
            "expected a full traceback (exc_info), not just a bare message"
        )


@pytest.mark.parametrize(
    "run_reader_loop",
    [run_stdout_reader_loop, run_stderr_reader_loop],
    ids=["stdout", "stderr"],
)
def test_closed_fds_are_detected_within_bounded_time(run_reader_loop):
    """Fix finding A (HIGH, Claude reviewer): unlike select.select()
    (fail-fast OSError when a monitored fd was closed out from under it),
    epoll silently drops a closed fd with no readiness event -- an
    unbounded sel.select() could block forever. Reproduces the
    reviewer's exact repro for BOTH reader loops (stderr is historically
    the more exposed one: round 2 found that the real `finally` block
    used to close shutdown_r/shutdown_w BEFORE stderr_thread had been
    joined at all, since stderr_thread.join() only ran after
    process.wait() back then -- now fixed by joining both threads before
    either fd is closed, but this test still exercises stderr
    specifically since it was the loop where that defect actually
    manifested).

    RED (pre-fix): no timeout, no liveness probe, no reader_failed
    parameter at all -- would hang past this test's join bound or raise
    a TypeError on the old signature.

    GREEN: detected within one poll interval (empirically ~0.0005s),
    flagged as abnormal.
    """
    data_r, data_w = os.pipe()
    thread, _sink, reader_failed, shutdown_r, shutdown_w = run_reader_loop(data_r)
    try:
        time.sleep(CLOSED_FD_RACE_SETTLE_SECONDS)
        os.close(data_r)
        os.close(shutdown_r)

        thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), (
            f"reader thread did not return within "
            f"{THREAD_JOIN_TIMEOUT_SECONDS}s after both fds were closed "
            f"out from under it -- finding A's unbounded epoll-silent-"
            f"drop hang"
        )
        assert reader_failed.is_set(), (
            "an fd closed without being observed as readable first must "
            "be flagged as an abnormal reader termination"
        )
    finally:
        close_quietly(data_w)
        close_quietly(shutdown_w)


class TestStderrReaderLoopCoverage:
    """Zero coverage previously existed on `_stderr_reader_loop` at all
    (both reviewers flagged this). Covers the `stderr_fd < 0` guard and
    the raw-text buffer-flush path; the closed-fd-race scenario is
    covered above (parametrized for both readers).
    """

    def test_negative_fd_guard_returns_immediately(self):
        from code_indexer.services.progress_subprocess_runner import (
            _stderr_reader_loop,
        )

        stderr_lines: list = []
        reader_failed = threading.Event()
        # Called directly (not via a thread): the guard returns before
        # ever touching a selector, so this is safe inline.
        _stderr_reader_loop(-1, -1, stderr_lines, "test_negative_fd", reader_failed)
        assert stderr_lines == []
        assert reader_failed.is_set() is False

    def test_buffer_flush_path_accumulates_raw_text(self):
        data_r, data_w = os.pipe()
        thread, stderr_lines, reader_failed, shutdown_r, shutdown_w = (
            run_stderr_reader_loop(data_r)
        )
        try:
            payload = b"some error output, no trailing newline"
            assert os.write(data_w, payload) == len(payload), (
                "expected a full, non-partial write to the pipe"
            )
            close_quietly(data_w)  # natural EOF

            thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            assert not thread.is_alive()
            assert reader_failed.is_set() is False
            assert stderr_lines == ["some error output, no trailing newline"]
        finally:
            close_quietly(data_r)
            close_quietly(shutdown_r)
            close_quietly(shutdown_w)


class _FlakyQueueWrapper:
    """Test-controlled fake collaborator (never the SUT) standing in for
    `line_queue`: raises once on a chosen call, then delegates to a real
    `queue.Queue` for every other call -- including the two finally-block
    calls (flush, sentinel), which must therefore still be attempted.
    """

    def __init__(self, fail_at_call: int):
        self._real: "queue_module.Queue" = queue_module.Queue()
        self._fail_at_call = fail_at_call
        self._calls = 0

    def put(self, item) -> None:
        self._calls += 1
        if self._calls == self._fail_at_call:
            raise RuntimeError("synthetic caller-side processing fault")
        self._real.put(item)

    def get_nowait(self):
        return self._real.get_nowait()


class TestCallerSideProcessingFailureStillGuaranteesSentinel:
    """Fix finding C (MEDIUM, Codex): `buf += chunk`, `.decode()`, and
    `line_queue.put()` in `_stdout_reader_loop`'s consumer loop sit
    OUTSIDE `_read_available_bytes`'s try/except. An exception there
    must still be caught, logged (with a full traceback), flagged, and
    must not prevent the flush/sentinel from each being independently
    attempted.

    RED (pre-fix): the caller-side loop body had no try/except at all --
    an exception there propagated out of the thread target uncaught
    (silent thread death), and the sentinel was never reached.
    """

    def test_processing_exception_is_caught_logged_flagged_and_flush_sentinel_attempted(
        self, caplog
    ):
        from code_indexer.services.progress_subprocess_runner import (
            _stdout_reader_loop,
        )

        data_r, data_w = os.pipe()
        shutdown_r, shutdown_w = os.pipe()
        # Fails on the FIRST put() call (the first real line); the
        # flush of the remaining buffer and the sentinel push are calls
        # #2 and #3 -- both must still be attempted.
        flaky_queue = _FlakyQueueWrapper(fail_at_call=1)
        reader_failed = threading.Event()

        with caplog.at_level(logging.ERROR):
            thread = threading.Thread(
                target=_stdout_reader_loop,
                args=(
                    data_r,
                    shutdown_r,
                    flaky_queue,
                    "test_flaky_queue",
                    reader_failed,
                ),
                daemon=True,
            )
            thread.start()
            try:
                payload = b"line one\nline two\n"
                assert os.write(data_w, payload) == len(payload), (
                    "expected a full, non-partial write to the pipe"
                )
                close_quietly(data_w)  # natural EOF

                thread.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
                assert not thread.is_alive(), (
                    "reader thread hung instead of catching the "
                    "caller-side processing failure"
                )
                assert reader_failed.is_set(), (
                    "a caller-side processing failure must be flagged via reader_failed"
                )
                assert flaky_queue._calls == 3, (
                    f"expected exactly 3 put() attempts (the failing "
                    f"line, the flush, and the sentinel -- flush and "
                    f"sentinel each independently attempted despite the "
                    f"first failure), got {flaky_queue._calls}"
                )
                # Call #1 (the "line one\n" put) raised and never reached
                # the real queue. Calls #2 (flush of the remaining "line
                # two\n") and #3 (sentinel) both succeeded, in that
                # order -- the flush runs before the sentinel push in
                # _stdout_reader_loop's finally block.
                assert drain_queue(flaky_queue) == ["line two\n", None], (
                    "expected the flush (call #2) then the sentinel "
                    "(call #3) to both land in the real underlying queue"
                )
            finally:
                close_quietly(data_r)
                close_quietly(shutdown_r)
                close_quietly(shutdown_w)

        assert "while processing captured data" in caplog.text, (
            f"expected the caller-side processing failure to be logged "
            f"with a clear message, got: {caplog.text!r}"
        )
        assert any(record.exc_info for record in caplog.records), (
            "expected a full traceback (exc_info), not just a bare message"
        )


class TestRaiseIfReaderFailed:
    """Codex round-2 Medium finding: `reader_failed` was previously only
    logged, and `run_with_popen_progress` still returned `high_water` as
    if the run succeeded -- a truncated stdout/stderr capture could be
    silently treated as a complete, trustworthy result. Fixed by
    extracting the decision into `_raise_if_reader_failed`, called from
    `_run_with_popen_progress_impl`'s tail (after the watchdog-kill and
    non-zero-exit checks, so those stay higher priority) -- a pure,
    directly-testable flag-check-and-raise with no process/timing logic
    at all (unlike the earlier-dropped `_handle_reader_crash`, this
    cannot reintroduce any Bug #1218 concern).
    """

    def test_raises_indexing_subprocess_error_when_reader_failed_is_set(self):
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            _raise_if_reader_failed,
        )

        reader_failed = threading.Event()
        reader_failed.set()

        with pytest.raises(IndexingSubprocessError) as exc_info:
            _raise_if_reader_failed(reader_failed, "test_label")

        assert "reader thread(s) terminated abnormally" in str(exc_info.value)
        assert "test_label" in str(exc_info.value)

    def test_is_a_no_op_when_reader_failed_is_clear(self):
        from code_indexer.services.progress_subprocess_runner import (
            _raise_if_reader_failed,
        )

        reader_failed = threading.Event()
        # Must not raise -- a healthy job with reader_failed never set
        # must be completely unaffected by this function's existence.
        _raise_if_reader_failed(reader_failed, "test_label")


def _wait_until_fd_closed(fd: int, timeout_seconds: float) -> bool:
    """Poll (real, non-mocked) until `fd` is closed or the timeout
    elapses. Uses the SUT's own `_fd_is_open` primitive -- the same
    check `_read_available_bytes`'s staleness probe relies on -- so this
    test observes exactly what the production code would observe.
    """
    from code_indexer.services.progress_subprocess_runner import _fd_is_open

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _fd_is_open(fd):
            return True
        time.sleep(0.02)
    return not _fd_is_open(fd)


class TestJoinReaderThreadsBeforeClosingShutdownPipe:
    """Bug #1774 round 3 (Codex finding): a join that actually times out
    must NEVER fall through to closing the shutdown fds -- that would
    reintroduce the exact fd-lifecycle hazard round 2 fixed, in
    precisely the case where it matters most (a reader thread confirmed
    still running). Fixed by having
    `_join_reader_threads_before_closing_shutdown_pipe` raise
    `IndexingSubprocessError` (and set `reader_failed`) instead of
    returning normally when either thread fails to join within the
    given timeout.

    Round 4 (Codex finding, coordinator sided with Codex over Claude's
    "acceptable leak" call): simply raising and abandoning the fds is
    itself a permanent resource leak -- the exact failure shape (small
    leaks compounding over weeks of uptime) motivating sibling Bug
    #1775. Fixed with a fire-and-forget daemon reaper that finishes
    joining the stuck reader(s) with no timeout, then closes the fds
    once that completes -- never blocking the main raise.

    RED (pre-fix, round 3): the function did not exist as an extracted,
    directly-testable unit -- ImportError. The inline logic it replaced
    unconditionally fell through to closing the fds on a timeout,
    without setting reader_failed either.

    GREEN (post-fix): a genuinely stuck (real, never-completing) thread
    causes a loud, distinct raise within a bounded, short
    `timeout_seconds` -- no real 30-second wait needed for this test.
    """

    def test_raises_and_flags_reader_failed_when_a_thread_never_joins(self):
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            _join_reader_threads_before_closing_shutdown_pipe,
        )

        never_stops = threading.Event()
        # A REAL thread that genuinely never completes until released --
        # not a mock, not a patched join(). This is what the function
        # must correctly detect and refuse to silently paper over.
        stuck_thread = threading.Thread(
            target=never_stops.wait,
            daemon=True,
        )
        stuck_thread.start()

        finished_thread = threading.Thread(target=lambda: None)
        finished_thread.start()
        finished_thread.join()

        shutdown_r, shutdown_w = os.pipe()
        reader_failed = threading.Event()
        try:
            with pytest.raises(IndexingSubprocessError) as exc_info:
                _join_reader_threads_before_closing_shutdown_pipe(
                    finished_thread,
                    stuck_thread,
                    reader_failed,
                    "test_label",
                    timeout_seconds=0.05,
                    shutdown_r=shutdown_r,
                    shutdown_w=shutdown_w,
                )

            assert "stderr reader could not be reaped" in str(exc_info.value), (
                f"expected the stuck stream (stderr) to be named in the "
                f"error, got: {exc_info.value}"
            )
            assert "test_label" in str(exc_info.value)
            assert reader_failed.is_set(), (
                "a timed-out join must also flag reader_failed"
            )
        finally:
            # Release the stuck thread so it doesn't leak past this test
            # -- the reaper it spawned should then close these fds on
            # its own (dedicated proof in the reaper test below); assert
            # that here too before cleanup, rather than discarding it,
            # so cleanup never races a reaper that might still be about
            # to close a since-recycled fd number.
            never_stops.set()
            stuck_thread.join(timeout=5.0)
            assert not stuck_thread.is_alive(), (
                "test cleanup: stuck thread must be releasable"
            )
            assert _wait_until_fd_closed(shutdown_r, timeout_seconds=5.0), (
                "reaper did not close shutdown_r during test cleanup"
            )
            close_quietly(shutdown_r)
            close_quietly(shutdown_w)

    def test_does_not_raise_when_both_threads_join_promptly(self):
        from code_indexer.services.progress_subprocess_runner import (
            _join_reader_threads_before_closing_shutdown_pipe,
        )

        thread_a = threading.Thread(target=lambda: None)
        thread_b = threading.Thread(target=lambda: None)
        thread_a.start()
        thread_b.start()

        shutdown_r, shutdown_w = os.pipe()
        reader_failed = threading.Event()
        try:
            # Must not raise, must not flag reader_failed -- proves the
            # round-3 fix doesn't turn healthy joins into false positives.
            _join_reader_threads_before_closing_shutdown_pipe(
                thread_a,
                thread_b,
                reader_failed,
                "test_label",
                timeout_seconds=5.0,
                shutdown_r=shutdown_r,
                shutdown_w=shutdown_w,
            )
            assert reader_failed.is_set() is False
        finally:
            # No reaper is spawned on the healthy path -- this test owns
            # closing the fds itself.
            close_quietly(shutdown_r)
            close_quietly(shutdown_w)

    def test_reaper_closes_shutdown_fds_once_the_stuck_reader_later_exits(self):
        """Bug #1774 round 4: the core reaper-fix verification. Proves
        (1) the main raise happens promptly, NOT blocked on the reaper
        (which is still joining the stuck thread at that moment); (2)
        the fds are therefore still open immediately after the raise;
        (3) once the stuck thread is released, the reaper -- running
        entirely on its own daemon thread -- eventually closes both
        fds. Codex's own repro technique: join-timeout probe, then
        check real fd state after the raise and again after release.
        """
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            _fd_is_open,
            _join_reader_threads_before_closing_shutdown_pipe,
        )

        never_stops = threading.Event()
        stuck_thread = threading.Thread(target=never_stops.wait, daemon=True)
        stuck_thread.start()

        finished_thread = threading.Thread(target=lambda: None)
        finished_thread.start()
        finished_thread.join()

        shutdown_r, shutdown_w = os.pipe()
        reader_failed = threading.Event()
        try:
            start = time.monotonic()
            with pytest.raises(IndexingSubprocessError):
                _join_reader_threads_before_closing_shutdown_pipe(
                    finished_thread,
                    stuck_thread,
                    reader_failed,
                    "test_label",
                    timeout_seconds=0.05,
                    shutdown_r=shutdown_r,
                    shutdown_w=shutdown_w,
                )
            elapsed_to_raise = time.monotonic() - start

            # (1) The raise must be prompt -- bounded by timeout_seconds,
            # not by however long the (still-running, never-released)
            # stuck thread takes to finish. The reaper's own join() has
            # NO timeout, so if the raise waited on it, this would hang
            # for the rest of the test suite's lifetime instead of
            # returning quickly.
            assert elapsed_to_raise < 2.0, (
                f"raise took {elapsed_to_raise:.2f}s -- must not block "
                f"on the reaper thread"
            )

            # (2) Immediately after the raise, the fds must still be
            # open: the reaper is still blocked joining the stuck
            # thread, so ownership of closing them has been handed off
            # but not yet exercised.
            assert _fd_is_open(shutdown_r), (
                "shutdown_r must still be open right after the raise -- "
                "the reaper has not been able to close it yet"
            )
            assert _fd_is_open(shutdown_w), (
                "shutdown_w must still be open right after the raise"
            )

            # (3) Release the stuck thread -- this is what the reaper's
            # own (untimed) join() is waiting on.
            never_stops.set()
            stuck_thread.join(timeout=5.0)
            assert not stuck_thread.is_alive()

            # The reaper should now finish its own join() (already
            # satisfied, since the thread is dead) and close both fds.
            assert _wait_until_fd_closed(shutdown_r, timeout_seconds=5.0), (
                "reaper did not close shutdown_r after the stuck reader exited"
            )
            assert _wait_until_fd_closed(shutdown_w, timeout_seconds=5.0), (
                "reaper did not close shutdown_w after the stuck reader exited"
            )
        finally:
            # Consistent with the sibling test above: wait for the
            # reaper to actually close the fds before this cleanup also
            # tries to close them -- if an earlier assertion in the try
            # block ever fails, jumping straight to close_quietly here
            # could race the reaper's own background os.close() call.
            # Deliberately best-effort here (unlike the try block's own
            # assertions on the same check): a slow/failed reaper during
            # cleanup should not mask whatever assertion failure is
            # already propagating out of this finally.
            never_stops.set()
            stuck_thread.join(timeout=5.0)
            _reaper_finished_during_cleanup = _wait_until_fd_closed(
                shutdown_r, timeout_seconds=5.0
            )
            del _reaper_finished_during_cleanup  # best-effort; see above
            close_quietly(shutdown_r)
            close_quietly(shutdown_w)


class TestSpawnReaperAndRaise:
    """Bug #1774 round 5 (Codex/Claude F1 finding): `Thread.start()` can
    itself raise `RuntimeError` under genuine thread/resource exhaustion
    -- precisely the degraded state this whole bug is about. Callers
    that catch `IndexingSubprocessError` specifically (e.g.
    golden_repo_manager.py, translating it to `GitOperationError`) must
    never see a raw `RuntimeError` escape instead just because the
    reaper itself couldn't be started. Uses the `start_reaper_thread`
    dependency-injection seam (defaults to the real
    `threading.Thread.start` in production) rather than monkeypatching
    any process-wide thread behavior.
    """

    def test_raises_indexing_subprocess_error_not_runtime_error_when_reaper_thread_start_fails(
        self,
    ):
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            _spawn_reaper_and_raise,
        )

        def failing_start(_thread: threading.Thread) -> None:
            # The exact exception a real thread/resource-exhaustion
            # condition raises from Thread.start().
            raise RuntimeError("can't start new thread")

        with pytest.raises(IndexingSubprocessError) as exc_info:
            _spawn_reaper_and_raise(
                stuck_threads=[],
                shutdown_r=-1,
                shutdown_w=-1,
                error_label="test_label",
                timed_out_stream_name="stderr",
                timeout_seconds=0.05,
                start_reaper_thread=failing_start,
            )

        assert not isinstance(exc_info.value, RuntimeError), (
            f"a raw RuntimeError must never escape past callers that "
            f"catch IndexingSubprocessError specifically, got: "
            f"{type(exc_info.value)}"
        )
        assert "stderr reader could not be reaped" in str(exc_info.value), (
            f"expected the same loud, distinct error message as the "
            f"success-then-raise path, got: {exc_info.value}"
        )
