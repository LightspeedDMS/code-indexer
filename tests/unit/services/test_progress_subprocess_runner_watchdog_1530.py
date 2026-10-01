"""Integration tests for the Issue #1530 watchdog wired into
`run_with_popen_progress` (Priority 2, final integration step).

All tests here spawn a REAL child process (via `sys.executable -c ...`)
that uses the REAL `ActivityBeacon`/`ActivityHeartbeatWriter` primitives --
no mocking of the beacon, the writer, the watchdog, or the subprocess.
This is the only way to genuinely validate the parent<->child heartbeat
file protocol end-to-end (per the issue's own instructions: "Mocks cannot
validate this failure class").

Bug #1218 invariant, re-proven at the subprocess boundary: a legitimately
slow-but-always-progressing child must never be killed, however long it
runs in total.
"""

import logging
import sys
import time

import pytest

from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
from code_indexer.services.progress_subprocess_runner import (
    IndexingWatchdogKillError,
    run_with_popen_progress,
)

_TEST_WATCHDOG_THRESHOLD_SECONDS = 0.5
_TEST_WEDGE_SLEEP_SECONDS = 30
_TEST_BOUND_SECONDS = 10.0


def _make_allocator() -> ProgressPhaseAllocator:
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(index_types=["semantic"], file_count=1, commit_count=0)
    return allocator


class TestWatchdogKillsWedgedSubprocess:
    """A child that starts a real heartbeat writer and then sleeps for a
    simulated-wedge duration (30s -- deliberately far longer than this
    test's own kill-detection bound) inside a single open beacon tick must
    be detected and killed within a bounded time after the staleness
    threshold, well before that 30s sleep would ever complete naturally.
    """

    def test_wedged_child_is_killed_and_raises_watchdog_error(self) -> None:
        child_script = (
            "import time\n"
            "from code_indexer.services.activity_beacon import ActivityBeacon\n"
            "from code_indexer.services.activity_heartbeat_writer import (\n"
            "    ActivityHeartbeatWriter,\n"
            "    ACTIVITY_HEARTBEAT_PATH_ENV,\n"
            ")\n"
            "import os\n"
            "heartbeat_path = os.environ[ACTIVITY_HEARTBEAT_PATH_ENV]\n"
            "beacon = ActivityBeacon()\n"
            "writer = ActivityHeartbeatWriter(\n"
            "    beacon=beacon, heartbeat_path=heartbeat_path, write_interval_seconds=0.05\n"
            ")\n"
            "writer.start()\n"
            'print(\'{"current": 0, "total": 1, "info": "wedging now"}\', flush=True)\n'
            "with beacon.tick('wedged_operation'):\n"
            f"    time.sleep({_TEST_WEDGE_SLEEP_SECONDS})\n"
        )
        command = [sys.executable, "-c", child_script]

        start = time.monotonic()
        with pytest.raises(IndexingWatchdogKillError):
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=_make_allocator(),
                progress_callback=None,
                all_stdout=[],
                all_stderr=[],
                cwd=None,
                stale_activity_timeout_seconds=_TEST_WATCHDOG_THRESHOLD_SECONDS,
            )
        elapsed = time.monotonic() - start

        assert elapsed < _TEST_BOUND_SECONDS, (
            f"watchdog took {elapsed}s to kill the wedged child -- should "
            f"be well under the full {_TEST_WEDGE_SLEEP_SECONDS}s wedge "
            f"duration"
        )


# Constants for TestNeverKillsSlowButProgressingSubprocess.
_SLOW_TICK_COUNT = 20
_SLOW_TICK_DURATION_SECONDS = 0.05
_SLOW_IDLE_GAP_SECONDS = 0.05
# Total runtime (~2s) comfortably exceeds the threshold (0.5s) several
# times over -- proving total elapsed time never itself triggers a kill.
_SLOW_WATCHDOG_THRESHOLD_SECONDS = 0.5


class TestNeverKillsSlowButProgressingSubprocess:
    """A child that keeps ticking (each tick well under the threshold,
    with idle gaps in between) for a TOTAL duration that comfortably
    exceeds the threshold several times over must never be killed --
    Bug #1218 invariant, re-proven at the real subprocess boundary.
    """

    def test_slow_progressing_child_is_never_killed(self) -> None:
        child_script = (
            "import time\n"
            "from code_indexer.services.activity_beacon import ActivityBeacon\n"
            "from code_indexer.services.activity_heartbeat_writer import (\n"
            "    ActivityHeartbeatWriter,\n"
            "    ACTIVITY_HEARTBEAT_PATH_ENV,\n"
            ")\n"
            "import os\n"
            "heartbeat_path = os.environ[ACTIVITY_HEARTBEAT_PATH_ENV]\n"
            "beacon = ActivityBeacon()\n"
            "writer = ActivityHeartbeatWriter(\n"
            "    beacon=beacon, heartbeat_path=heartbeat_path, write_interval_seconds=0.02\n"
            ")\n"
            "writer.start()\n"
            f"for i in range({_SLOW_TICK_COUNT}):\n"
            "    with beacon.tick('slow_progress_step'):\n"
            f"        time.sleep({_SLOW_TICK_DURATION_SECONDS})\n"
            f'    print(f\'{{{{"current": {{i}}, "total": {_SLOW_TICK_COUNT}, '
            '"info": "progressing"}}\', flush=True)\n'
            f"    time.sleep({_SLOW_IDLE_GAP_SECONDS})\n"
        )
        command = [sys.executable, "-c", child_script]
        all_stdout: list = []

        result = run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=_make_allocator(),
            progress_callback=None,
            all_stdout=all_stdout,
            all_stderr=[],
            cwd=None,
            stale_activity_timeout_seconds=_SLOW_WATCHDOG_THRESHOLD_SECONDS,
        )

        assert isinstance(result, int)
        assert any("progressing" in line for line in all_stdout), (
            "expected to see progress lines from the full run, proving the "
            "child was never killed mid-way"
        )


class TestKillIsObservableViaLogging:
    """The kill must be observable -- a distinguishable exception type
    AND a logged record with enough detail (pid/reason/label) to be
    useful in production logs -- never a silent process disappearance.
    """

    def test_wedged_kill_logs_error_with_details(self, caplog) -> None:
        child_script = (
            "import time\n"
            "from code_indexer.services.activity_beacon import ActivityBeacon\n"
            "from code_indexer.services.activity_heartbeat_writer import (\n"
            "    ActivityHeartbeatWriter,\n"
            "    ACTIVITY_HEARTBEAT_PATH_ENV,\n"
            ")\n"
            "import os\n"
            "heartbeat_path = os.environ[ACTIVITY_HEARTBEAT_PATH_ENV]\n"
            "beacon = ActivityBeacon()\n"
            "writer = ActivityHeartbeatWriter(\n"
            "    beacon=beacon, heartbeat_path=heartbeat_path, write_interval_seconds=0.05\n"
            ")\n"
            "writer.start()\n"
            "with beacon.tick('wedged_operation'):\n"
            f"    time.sleep({_TEST_WEDGE_SLEEP_SECONDS})\n"
        )
        command = [sys.executable, "-c", child_script]

        with caplog.at_level(logging.ERROR):
            with pytest.raises(IndexingWatchdogKillError) as exc_info:
                run_with_popen_progress(
                    command=command,
                    phase_name="semantic",
                    allocator=_make_allocator(),
                    progress_callback=None,
                    all_stdout=[],
                    all_stderr=[],
                    cwd=None,
                    stale_activity_timeout_seconds=_TEST_WATCHDOG_THRESHOLD_SECONDS,
                )

        assert isinstance(exc_info.value, IndexingWatchdogKillError)
        assert any("wedged" in record.message for record in caplog.records), (
            "expected an ERROR-level log record describing the wedged subprocess"
        )


# Constants for TestBootstrapGracePeriod.
# The delay must exceed the watchdog's fixed 2.0s in-loop check interval
# (see _WATCHDOG_CHECK_INTERVAL_SECONDS in progress_subprocess_runner.py)
# so at least one real watchdog check genuinely observes the heartbeat
# file still absent -- proving the grace period, not merely "the process
# finished before the watchdog ever looked".
_BOOTSTRAP_DELAY_SECONDS = 2.5
_BOOTSTRAP_THRESHOLD_SECONDS = 4.0
_BOOTSTRAP_TEST_BOUND_SECONDS = 15.0


class TestBootstrapGracePeriod:
    """A child that takes a moment to write its first heartbeat record
    (simulating a brief, legitimate startup delay before any ticking
    begins) must not be killed during that grace window -- fail-safe on
    PROLONGED absence (issue design point 4), never fail-open forever, but
    also never fail-closed instantly on a normal startup delay.
    """

    def test_delayed_first_heartbeat_write_is_not_killed(self) -> None:
        child_script = (
            "import time\n"
            f"time.sleep({_BOOTSTRAP_DELAY_SECONDS})\n"
            "from code_indexer.services.activity_beacon import ActivityBeacon\n"
            "from code_indexer.services.activity_heartbeat_writer import (\n"
            "    ActivityHeartbeatWriter,\n"
            "    ACTIVITY_HEARTBEAT_PATH_ENV,\n"
            ")\n"
            "import os\n"
            "heartbeat_path = os.environ[ACTIVITY_HEARTBEAT_PATH_ENV]\n"
            "beacon = ActivityBeacon()\n"
            "writer = ActivityHeartbeatWriter(\n"
            "    beacon=beacon, heartbeat_path=heartbeat_path, write_interval_seconds=0.05\n"
            ")\n"
            "writer.start()\n"
            "with beacon.tick('post_bootstrap_work'):\n"
            "    time.sleep(0.1)\n"
            "print('done', flush=True)\n"
        )
        command = [sys.executable, "-c", child_script]
        all_stdout: list = []

        start = time.monotonic()
        result = run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=_make_allocator(),
            progress_callback=None,
            all_stdout=all_stdout,
            all_stderr=[],
            cwd=None,
            stale_activity_timeout_seconds=_BOOTSTRAP_THRESHOLD_SECONDS,
        )
        elapsed = time.monotonic() - start

        # If the watchdog had incorrectly killed the child during the
        # bootstrap grace window, run_with_popen_progress would have
        # RAISED IndexingWatchdogKillError instead of returning at all --
        # so reaching this line already proves the child was NOT killed
        # (a nonzero subprocess exit would likewise have raised
        # IndexingSubprocessError). `result` itself is the returned
        # high-water PROGRESS PERCENTAGE (from the phase_start emission),
        # not the child's exit code -- it is 0 here because this is the
        # sole/first phase and the child never printed a JSON progress
        # line. The "done" marker is the real proof that the child's
        # post-bootstrap work actually ran to completion.
        assert any("done" in line for line in all_stdout), (
            "expected to see the child's own completion marker, proving it "
            "ran its post-bootstrap work to completion rather than being "
            "killed during the grace window"
        )
        assert result == 0, (
            f"expected phase_start's high-water value (0 for the first phase), got {result}"
        )
        assert elapsed < _BOOTSTRAP_TEST_BOUND_SECONDS, (
            f"test itself took {elapsed}s -- unexpectedly long, investigate"
        )


# Constants for TestWatchdogSurvivesStdoutEof.
_EOF_CHILD_SLEEP_SECONDS = 25
_EOF_WATCHDOG_THRESHOLD_SECONDS = 1.0
_EOF_CALL_BOUND_SECONDS = 15.0
# Teardown bound: only ever reached when the regression is present (the
# runner thread is still blocked). Covers the child's whole self-terminating
# sleep plus margin, so neither the thread nor the real child outlives the
# test. On the passing path this join returns immediately.
_EOF_TEARDOWN_JOIN_SECONDS = _EOF_CHILD_SLEEP_SECONDS + 5.0

# Constants for TestHeartbeatFileCleanedUpOnExceptionPath.
_CLEANUP_POLL_TIMEOUT_SECONDS = 5.0
_CLEANUP_POLL_SLEEP_SECONDS = 0.05
_CLEANUP_WATCHDOG_THRESHOLD_SECONDS = 60.0


def _eof_child_script() -> str:
    """Child that closes stdout (natural EOF) then wedges, still alive."""
    return (
        "import os, sys, time\n"
        "sys.stdout.flush()\n"
        "os.close(1)\n"
        f"time.sleep({_EOF_CHILD_SLEEP_SECONDS})\n"
    )


def _heartbeat_child_script(existence_sentinel_path: str) -> str:
    """Child that starts a REAL heartbeat writer, proves the heartbeat file
    genuinely exists (ActivityHeartbeatWriter.start() writes the bootstrap
    record synchronously) by dropping `existence_sentinel_path`, then emits
    one progress line and exits.

    The sentinel is what stops the parent's cleanup assertion from passing
    vacuously: "no leftover heartbeat file" only proves cleanup if a
    heartbeat file was demonstrably created in the first place.
    """
    return (
        "import os, sys, time\n"
        "from code_indexer.services.activity_beacon import ActivityBeacon\n"
        "from code_indexer.services.activity_heartbeat_writer import (\n"
        "    ActivityHeartbeatWriter,\n"
        "    ACTIVITY_HEARTBEAT_PATH_ENV,\n"
        ")\n"
        "hb = os.environ[ACTIVITY_HEARTBEAT_PATH_ENV]\n"
        "beacon = ActivityBeacon()\n"
        "writer = ActivityHeartbeatWriter(\n"
        "    beacon=beacon, heartbeat_path=hb, write_interval_seconds=0.05\n"
        ")\n"
        "writer.start()\n"
        "if os.path.exists(hb):\n"
        f"    open({existence_sentinel_path!r}, 'w').close()\n"
        'print(\'{"current": 1, "total": 2, "info": "x"}\', flush=True)\n'
        "writer.stop()\n"
    )


def _wait_for_no_leftovers(pattern: str, timeout: float) -> list:
    """Bounded poll returning whatever still matches `pattern` at the end.

    Uses the module-scope `time` import declared at the top of this file.
    """
    import glob

    deadline = time.monotonic() + timeout
    leftovers = glob.glob(pattern)
    while leftovers and time.monotonic() < deadline:
        time.sleep(_CLEANUP_POLL_SLEEP_SECONDS)
        leftovers = glob.glob(pattern)
    return leftovers


def _exploding_callback(pct, phase=None, detail=None) -> None:
    """A caller-supplied progress callback that raises -- entirely
    realistic (the server's callbacks write to the job manager/DB).

    Deliberately lets the initial `phase_start` emission (pct == 0) through
    and raises only on a REAL progress line: raising on phase_start would
    abort before the child had written anything at all, so the cleanup
    assertion would exercise nothing.
    """
    if pct > 0:
        raise RuntimeError("progress callback blew up")


class TestWatchdogSurvivesStdoutEof:
    """Issue #1530 design point 8: the watchdog must keep checking while
    the child is alive, INDEPENDENT of the stdout reader's own completion.

    A child that closes its stdout (natural EOF) while remaining alive and
    wedged makes the main poll loop break on the reader's sentinel and fall
    straight into an UNBOUNDED `process.wait()`. That is precisely the
    parent-side blind spot the issue flagged: without a watchdog check
    covering that wait, `run_with_popen_progress` hangs for the child's
    entire wedge duration and the mechanism never fires.

    Real subprocess, real EOF, real kill -- no mocks.
    """

    def test_wedged_child_that_closed_stdout_is_still_killed(self) -> None:
        import queue
        import threading

        outcomes: "queue.Queue" = queue.Queue()

        def _run() -> None:
            try:
                run_with_popen_progress(
                    command=[sys.executable, "-c", _eof_child_script()],
                    phase_name="semantic",
                    allocator=_make_allocator(),
                    progress_callback=None,
                    all_stdout=[],
                    all_stderr=[],
                    cwd=None,
                    stale_activity_timeout_seconds=_EOF_WATCHDOG_THRESHOLD_SECONDS,
                )
                outcomes.put(("returned", None))
            except BaseException as exc:  # noqa: BLE001 - published for assertion
                outcomes.put(("raised", exc))

        runner_thread = threading.Thread(target=_run, daemon=True)
        runner_thread.start()
        try:
            try:
                outcome, exc = outcomes.get(timeout=_EOF_CALL_BOUND_SECONDS)
            except queue.Empty:
                raise AssertionError(
                    "run_with_popen_progress never returned -- the watchdog "
                    "stopped checking once the stdout reader hit EOF and the "
                    "call blocked in an unbounded process.wait()"
                )
            assert outcome == "raised" and isinstance(exc, IndexingWatchdogKillError), (
                f"expected IndexingWatchdogKillError, got {outcome}: {exc!r}"
            )
        finally:
            # Bounded teardown: on the failing path the runner thread is
            # still blocked on the child's own self-terminating sleep --
            # wait it out so neither the thread nor the child outlives this
            # test. Returns immediately on the passing path.
            runner_thread.join(timeout=_EOF_TEARDOWN_JOIN_SECONDS)


class TestHeartbeatFileCleanedUpOnExceptionPath:
    """The heartbeat file must be removed on EVERY exit path, including an
    exception raised from inside the progress loop. Leaking one file per
    failed invocation slowly fills the node's temp dir.
    """

    def test_heartbeat_file_is_removed_when_progress_callback_raises(
        self, tmp_path
    ) -> None:
        import os

        heartbeat_dir = str(tmp_path)
        sentinel = str(tmp_path / "heartbeat_existed.marker")
        with pytest.raises(RuntimeError, match="blew up"):
            run_with_popen_progress(
                command=[sys.executable, "-c", _heartbeat_child_script(sentinel)],
                phase_name="semantic",
                allocator=_make_allocator(),
                progress_callback=_exploding_callback,
                all_stdout=[],
                all_stderr=[],
                cwd=None,
                env=dict(os.environ),
                stale_activity_timeout_seconds=_CLEANUP_WATCHDOG_THRESHOLD_SECONDS,
                heartbeat_dir=heartbeat_dir,
            )

        assert os.path.exists(sentinel), (
            "the child never observed its own heartbeat file -- the cleanup "
            "assertion below would be vacuous"
        )
        leftovers = _wait_for_no_leftovers(
            os.path.join(heartbeat_dir, "cidx_activity_heartbeat_*.json"),
            _CLEANUP_POLL_TIMEOUT_SECONDS,
        )
        assert not leftovers, (
            f"heartbeat file(s) leaked on the exception path: {leftovers}"
        )
