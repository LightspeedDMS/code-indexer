"""Bug #1746 Change 5: run_cancellable_subprocess() must stream ERROR-level
child output lines to the parent's log store WHILE the child is still
running -- not only after wait() returns and the buffered
stdout/stderr strings are assembled into the final CompletedProcess.

Root cause (production incident, GitHub issue #1746): _drain_stream()
buffered every line into an in-memory list with no side effect until the
child exited, so the server's log store (admin_logs_query) never saw a
per-file `logger.error()` call from inside a long-running `cidx index`
child while it was still hung -- total silence for the entire 2h13m
incident window, even though the child was logging errors the whole time.

Mocking policy: NO process mocks (matches the established convention in
this module's sibling test_cancellable_subprocess_1342.py) -- a REAL bash
child process is spawned, and a REAL logging.Handler observes the parent
module's own logger.
"""

import logging
import threading
import time

from code_indexer.server.utils.cancellable_subprocess import (
    run_cancellable_subprocess,
)

_MODULE_LOGGER_NAME = "code_indexer.server.utils.cancellable_subprocess"


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class TestChange5LiveErrorLineStreaming:
    """AC: Given the indexer child subprocess emits an ERROR-level log
    line during execution, when the parent server is tracking that job,
    then the error becomes visible via the log store WHILE the child
    process is still alive -- not only after wait() returns."""

    def test_error_line_visible_before_child_process_exits(self) -> None:
        target_logger = logging.getLogger(_MODULE_LOGGER_NAME)
        handler = _CapturingHandler()
        original_level = target_logger.level
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)

        distinctive_marker = "ERROR distinctive-marker-1746-live-stream"
        # Child: print a normal line, sleep briefly, emit the distinctive
        # ERROR line to stderr, THEN sleep much longer before exiting --
        # this is the window the test proves the parent sees the line in.
        script = (
            "echo starting; "
            "sleep 0.2; "
            f"echo '{distinctive_marker}' 1>&2; "
            "sleep 3; "
            "echo done"
        )

        result_holder: dict = {}
        run_thread = threading.Thread(
            target=lambda: result_holder.update(
                result=run_cancellable_subprocess(
                    ["bash", "-c", script],
                    cwd="/tmp",
                    poll_interval=0.05,
                )
            ),
            daemon=True,
        )
        run_thread.start()

        try:
            # Bounded active poll -- never a bare sleep-then-check.
            deadline = time.monotonic() + 4.0
            seen = False
            while time.monotonic() < deadline:
                if any(distinctive_marker in m for m in handler.messages):
                    seen = True
                    break
                time.sleep(0.05)

            assert seen, (
                "the distinctive ERROR line never became visible via the "
                "parent module's logger while the child subprocess was "
                "still running"
            )
            # The child sleeps 3s AFTER emitting the line -- if we saw it
            # this early, the run_cancellable_subprocess() call (and thus
            # the child) MUST still be in flight.
            assert run_thread.is_alive(), (
                "the ERROR line became visible only after the child "
                "process had already finished -- expected it WHILE the "
                "child was still alive (still sleeping)"
            )
        finally:
            run_thread.join(timeout=10)
            target_logger.removeHandler(handler)
            target_logger.setLevel(original_level)

        assert result_holder["result"].returncode == 0
        assert distinctive_marker in result_holder["result"].stderr


class TestChange5DetectsErrorLinesOnStdoutToo:
    """B4 (code review finding, round 2): the real `cidx index` child
    process -- the exact path Change 5 exists to observe -- writes its
    ERROR-level output to STDOUT, not stderr (measured live by the
    reviewer: 2 ERROR lines on stdout, 0 on stderr, for a real child).
    Restricting detection to stderr-only made Change 5 structurally dead
    in production. The anchored word-boundary match (M2's real fix) is
    what prevents the false-positive on "ERROR_CODES.py" -- NOT the
    stream restriction -- so detection must work on BOTH streams."""

    def test_error_line_on_stdout_visible_before_child_process_exits(self) -> None:
        target_logger = logging.getLogger(_MODULE_LOGGER_NAME)
        handler = _CapturingHandler()
        original_level = target_logger.level
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)

        distinctive_marker = "ERROR distinctive-marker-1746-stdout-stream"
        # Same shape as the stderr positive test above, but WITHOUT
        # "1>&2" -- this line goes to stdout, exactly where a real cidx
        # index child's ERROR-level output actually lands.
        script = (
            f"echo starting; sleep 0.2; echo '{distinctive_marker}'; sleep 3; echo done"
        )

        result_holder: dict = {}
        run_thread = threading.Thread(
            target=lambda: result_holder.update(
                result=run_cancellable_subprocess(
                    ["bash", "-c", script],
                    cwd="/tmp",
                    poll_interval=0.05,
                )
            ),
            daemon=True,
        )
        run_thread.start()

        try:
            deadline = time.monotonic() + 4.0
            seen = False
            while time.monotonic() < deadline:
                if any(distinctive_marker in m for m in handler.messages):
                    seen = True
                    break
                time.sleep(0.05)

            assert seen, (
                "the distinctive ERROR line on STDOUT never became "
                "visible via the parent module's logger while the child "
                "subprocess was still running"
            )
            assert run_thread.is_alive(), (
                "the ERROR line became visible only after the child "
                "process had already finished -- expected it WHILE the "
                "child was still alive (still sleeping)"
            )
        finally:
            run_thread.join(timeout=10)
            target_logger.removeHandler(handler)
            target_logger.setLevel(original_level)

        assert result_holder["result"].returncode == 0
        assert distinctive_marker in result_holder["result"].stdout


class TestChange5DoesNotMisclassifyOrdinaryLines:
    """M2 (code review finding): the "ERROR" substring match was over-eager
    -- it matched on both stdout and stderr, so an ordinary line merely
    CONTAINING "ERROR" as part of a longer identifier (e.g. a filename
    like "ERROR_CODES.py" in Rich progress output) would falsely trigger
    a parent-side logger.error() call, which per this project's Story
    #1122 can fail the E2E log-audit gate on a non-allowlisted entry.

    B4 (code review finding, round 2) superseded M2's original stream
    restriction: an early fix anchored the match to a real word-boundary
    ERROR token AND restricted detection to stderr only -- but the real
    `cidx index` child writes its ERROR-level output to STDOUT, not
    stderr, making that restriction structurally block detection in
    production (see TestChange5DetectsErrorLinesOnStdoutToo). The
    anchored word-boundary regex ALONE (checked on both streams) is what
    correctly rejects "ERROR_CODES.py" while still detecting a genuine
    ERROR-level line on either stream."""

    def test_error_substring_inside_a_filename_on_stdout_is_not_misclassified(
        self,
    ) -> None:
        target_logger = logging.getLogger(_MODULE_LOGGER_NAME)
        handler = _CapturingHandler()
        original_level = target_logger.level
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)

        # "ERROR" appears as a SUBSTRING of a filename, on STDOUT (not
        # stderr) -- exactly the false-positive shape M2 describes.
        script = (
            "echo 'processing ERROR_CODES.py'; "
            "echo 'no problems here, all clear'; "
            "sleep 0.3"
        )

        try:
            result = run_cancellable_subprocess(
                ["bash", "-c", script],
                cwd="/tmp",
                poll_interval=0.05,
            )
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(original_level)

        assert result.returncode == 0
        assert "ERROR_CODES.py" in result.stdout
        assert not any("ERROR_CODES" in m for m in handler.messages), (
            f"a filename merely containing the substring 'ERROR' on "
            f"stdout must NOT be misclassified as a live error line, "
            f"but the parent logger captured: {handler.messages!r}"
        )
