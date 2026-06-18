"""
Unit tests for shared progress subprocess runner utilities.

Story #482: Extend Real-Time Progress Reporting to All User-Facing Indexing Paths.
Step 1: Extract _run_with_popen_progress and _gather_repo_metrics from
golden_repo_manager.py into a new shared module
src/code_indexer/services/progress_subprocess_runner.py.

Tests cover:
- run_with_popen_progress: JSON progress lines map to progress_callback calls
- run_with_popen_progress: non-JSON lines are collected without crashing
- run_with_popen_progress: non-zero exit raises GoldenRepoError with stderr
- run_with_popen_progress: None progress_callback is safe (no AttributeError)
- gather_repo_metrics: returns (0, 0) gracefully for non-git directories
- gather_repo_metrics: returns positive counts for real git repos
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


class TestRunWithPopenProgress:
    """Tests for run_with_popen_progress shared utility."""

    def test_json_progress_lines_invoke_callback(self):
        """
        JSON progress lines emitted by the subprocess must be parsed and
        forwarded to progress_callback as phase-mapped global values.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=100,
            commit_count=0,
        )

        received = []

        def cb(pct, phase=None, detail=None):
            received.append(pct)

        # Script that emits JSON progress lines then exits 0
        script = (
            "import json, sys\n"
            'print(json.dumps({"current": 0, "total": 4, "info": "start"}))\n'
            'print(json.dumps({"current": 2, "total": 4, "info": "mid"}))\n'
            'print(json.dumps({"current": 4, "total": 4, "info": "done"}))\n'
        )
        command = [sys.executable, "-c", script]
        all_stdout = []  # type: ignore[var-annotated]
        all_stderr = []  # type: ignore[var-annotated]

        run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=None,
        )

        # Should have received progress values (phase_start + 3 mapped values)
        assert len(received) >= 3, f"Expected >= 3 callbacks, got {received}"
        # First value is phase_start (0 for first phase)
        assert received[0] == 0
        # Final value should be the end of the semantic phase.
        # Note: "cow" is always appended by calculate_weights, so semantic
        # phase_end < 100.  The mapped 4/4 value equals semantic phase_end.
        expected_end = int(allocator.phase_end("semantic"))
        assert received[-1] == expected_end, (
            f"Expected {expected_end} (semantic phase end), got {received[-1]}. "
            f"Full sequence: {received}"
        )

    def test_non_json_lines_accumulated_not_parsed(self):
        """
        Non-JSON lines from stdout must be accumulated in all_stdout but
        must NOT cause a crash or spurious progress callback.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        received = []

        def cb(pct, phase=None, detail=None):
            received.append(pct)

        script = (
            "import json\n"
            'print("not json at all")\n'
            'print("also not json")\n'
            'print(json.dumps({"current": 1, "total": 1, "info": "done"}))\n'
        )
        command = [sys.executable, "-c", script]
        all_stdout = []  # type: ignore[var-annotated]
        all_stderr = []  # type: ignore[var-annotated]

        run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=cb,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=None,
        )

        # Non-JSON lines accumulated in all_stdout
        combined = "".join(all_stdout)
        assert "not json at all" in combined
        assert "also not json" in combined

        # At least the final JSON line triggered a callback
        assert len(received) >= 1

    def test_nonzero_exit_raises_indexing_subprocess_error(self):
        """
        A subprocess that exits non-zero must raise IndexingSubprocessError
        (defined in progress_subprocess_runner itself) with stderr content in
        the message.  run_with_popen_progress must NOT import GoldenRepoError
        from golden_repo_manager — callers are responsible for re-raising as
        the appropriate domain error.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
            IndexingSubprocessError,
        )
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        script = (
            'import sys\nsys.stderr.write("something went wrong\\n")\nsys.exit(1)\n'
        )
        command = [sys.executable, "-c", script]
        all_stdout = []  # type: ignore[var-annotated]
        all_stderr = []  # type: ignore[var-annotated]

        with pytest.raises(IndexingSubprocessError) as exc_info:
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=all_stderr,
                cwd=None,
            )

        assert "something went wrong" in str(exc_info.value)

    def test_no_circular_import_from_golden_repo_manager(self):
        """
        progress_subprocess_runner must be importable without importing from
        golden_repo_manager.  This verifies there is no circular dependency:
        progress_subprocess_runner is a utility used BY golden_repo_manager,
        not the other way around.

        Uses subprocess isolation to get a truly fresh Python process, since
        the same pytest session will have already loaded golden_repo_manager
        from other tests, making sys.modules manipulation unreliable.
        """
        import sys
        from pathlib import Path

        src_dir = str(Path(__file__).parent.parent.parent.parent / "src")
        check_script = (
            "import sys\n"
            f"sys.path.insert(0, {src_dir!r})\n"
            "import code_indexer.services.progress_subprocess_runner\n"
            "loaded = [k for k in sys.modules if 'golden_repo_manager' in k]\n"
            "if loaded:\n"
            "    print('FAIL: ' + str(loaded))\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", check_script],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "progress_subprocess_runner imported golden_repo_manager as a side "
            f"effect, creating a circular dependency.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "OK" in result.stdout

    def test_none_progress_callback_is_safe(self):
        """
        Passing progress_callback=None must not raise AttributeError.
        The function should complete without calling any callback.
        """
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )
        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        script = 'import json; print(json.dumps({"current": 1, "total": 1}))'
        command = [sys.executable, "-c", script]
        all_stdout = []  # type: ignore[var-annotated]
        all_stderr = []  # type: ignore[var-annotated]

        # Must not raise
        run_with_popen_progress(
            command=command,
            phase_name="semantic",
            allocator=allocator,
            progress_callback=None,
            all_stdout=all_stdout,
            all_stderr=all_stderr,
            cwd=None,
        )


class TestSigtermErrorDetails:
    """Tests for Bug #934: SIGTERM (returncode -15) must always appear in error_details."""

    def test_sigterm_exit_error_details_contains_signal_info(self):
        """
        When subprocess returncode is -15 (SIGTERM) and stderr is empty but stdout
        has content (e.g. startup banner), the raised IndexingSubprocessError must
        contain signal-identifying text so the refresh_scheduler SIGTERM matcher works.
        The startup banner text must NOT be the only content.
        """
        import sys

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Script prints banner to stdout (like cidx startup), then kills itself with SIGTERM.
        # stderr stays empty. Simulates cidx being SIGTERMed while it just printed its banner.
        script = (
            "import os, signal, sys\n"
            "sys.stdout.write('cidx startup banner line 1\\n')\n"
            "sys.stdout.write('cidx startup banner line 2\\n')\n"
            "sys.stdout.flush()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

        with pytest.raises(IndexingSubprocessError) as exc_info:
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=all_stderr,
                cwd=None,
            )

        error_msg = str(exc_info.value)
        # Bug #934: signal info must be present — "Exit code -15" is what refresh_scheduler matches
        assert "Exit code -15" in error_msg, (
            f"Expected 'Exit code -15' in error message for SIGTERM kill, "
            f"but got: {error_msg!r}. The startup banner must not be the only content."
        )

    def test_negative_returncode_signal_info_always_present(self):
        """
        For any negative returncode (not just -15), the error_details must
        contain 'Exit code <N>' so that callers can reliably match on returncode.
        When stderr is empty and stdout has content, the signal info still takes precedence.
        """
        import sys

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Script prints stdout banner, then exits with SIGKILL equivalent (-9) by using
        # os.kill with SIGKILL.

        script = (
            "import os, signal, sys\n"
            "sys.stdout.write('some startup output\\n')\n"
            "sys.stdout.flush()\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

        with pytest.raises(IndexingSubprocessError) as exc_info:
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=all_stderr,
                cwd=None,
            )

        error_msg = str(exc_info.value)
        # For SIGKILL (-9), error must contain "Exit code -9"
        assert "Exit code -9" in error_msg, (
            f"Expected 'Exit code -9' in error for SIGKILL, got: {error_msg!r}"
        )

    def test_refresh_scheduler_sigterm_detection_matches_new_format(self):
        """
        The refresh_scheduler.py SIGTERM detection at line 1683 checks for
        'Exit code -15' in the error message. After Bug #934 fix, the
        IndexingSubprocessError raised for returncode=-15 must contain that
        substring so the routing works correctly.
        This test verifies the produced error message is compatible with the
        refresh_scheduler matcher without importing refresh_scheduler itself.
        """
        import sys

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Simulate cidx being SIGTERMed with non-empty stdout (startup banner) and empty stderr
        script = (
            "import os, signal, sys\n"
            "sys.stdout.write('Code Indexer v9.x.y - startup banner\\n')\n"
            "sys.stdout.flush()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

        error_msg = ""
        with pytest.raises(IndexingSubprocessError) as exc_info:
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=all_stderr,
                cwd=None,
            )
        error_msg = str(exc_info.value)

        # This is exactly what refresh_scheduler.py:1683 checks:
        sigterm_detected = "Exit code -15" in error_msg or "returncode=-15" in error_msg
        assert sigterm_detected, (
            f"refresh_scheduler SIGTERM detection would FAIL for error: {error_msg!r}. "
            f"SIGTERM jobs would be mis-routed to ERROR path and counted as failed_jobs."
        )


class TestFdWedgeResistance:
    """Tests for BUG 1: grandchild fd-wedge must not block run_with_popen_progress.

    A grandchild spawned by the child process inherits the write-end of the
    stdout PIPE (when close_fds=False), keeping it open even after the child
    exits.  An EOF-dependent readline loop blocks indefinitely waiting for an
    EOF that never arrives while the grandchild is alive.

    The fix has two complementary parts:
    1. start_new_session=True on the Popen places the child and its descendants
       in a new process group, enabling the watchdog to kill the whole group
       (os.killpg) on timeout.  However, start_new_session=True alone does NOT
       prevent grandchildren from inheriting the pipe fd -- they still hold it
       open after the child exits.
    2. Poll-aware read loop (selectors + process.poll()): the loop no longer
       waits for pipe EOF.  Instead it checks process.poll() every 0.1s and
       stops (with a final drain) as soon as the child has exited.  This is
       the mechanism that makes termination fast regardless of grandchildren.
    """

    # Maximum wall-clock seconds the call is allowed to take.
    # The grandchild sleeps for 8s; without the fix the call would take ~8s.
    # With the fix it should complete in well under 2s.
    MAX_ALLOWED_SECONDS = 4.0
    GRANDCHILD_SLEEP_SECONDS = 8

    def test_grandchild_fd_wedge_does_not_block(self):
        """run_with_popen_progress must complete promptly even when a grandchild
        spawned by the child holds the stdout PIPE write-end open.

        The fix is a poll-aware read loop (selectors + process.poll()): the
        loop terminates when the child exits, not when the pipe reaches EOF.
        start_new_session=True is also set but is NOT sufficient on its own --
        grandchildren still inherit the pipe fd and hold it open.

        Uses a real sleeping subprocess (anti-mock) as the grandchild.
        """
        import sys
        import time

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Script that:
        # 1. Prints a progress JSON line
        # 2. Spawns a grandchild WITHOUT close_fds (inherits all fds including
        #    the stdout PIPE write-end)
        # 3. Exits immediately (child exits at ~0.1s)
        # Grandchild sleeps for GRANDCHILD_SLEEP_SECONDS, keeping inherited
        # stdout PIPE write-end open.  Without the poll-aware loop the EOF-
        # dependent readline would block until the grandchild dies (~8s).
        script = (
            "import json, subprocess, sys, time\n"
            'print(json.dumps({"current": 1, "total": 1, "info": "done"}), flush=True)\n'
            f"grandchild = subprocess.Popen(['sleep', '{self.GRANDCHILD_SLEEP_SECONDS}'], close_fds=False)\n"
            "sys.exit(0)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

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

        assert elapsed < self.MAX_ALLOWED_SECONDS, (
            f"run_with_popen_progress took {elapsed:.2f}s — grandchild fd-wedge "
            f"is blocking the readline loop (expected < {self.MAX_ALLOWED_SECONDS}s). "
            f"Fix: add start_new_session=True to the Popen in run_with_popen_progress."
        )

    def test_failed_child_with_grandchild_raises_fast(self):
        """When child exits non-zero AND a grandchild holds the pipe, the
        exception must be raised within MAX_ALLOWED_SECONDS, not 300s+.

        This simulates the 'not initialized' cidx index failure that was
        swallowed because the job never went terminal.

        The fix is the poll-aware read loop: once process.poll() returns
        non-None (child exited with non-zero), the loop stops immediately,
        and the non-zero returncode triggers IndexingSubprocessError promptly.
        start_new_session=True is also set but does not close the inherited
        pipe fd held by the grandchild -- only the poll-aware loop matters here.
        """
        import sys
        import time

        import pytest

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Child prints an error to stderr (like 'cidx index: not initialized'),
        # spawns a grandchild holding the pipe, then exits non-zero.
        script = (
            "import subprocess, sys\n"
            "sys.stderr.write('Project is not initialized\\n')\n"
            f"subprocess.Popen(['sleep', '{self.GRANDCHILD_SLEEP_SECONDS}'], close_fds=False)\n"
            "sys.exit(1)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

        start = time.monotonic()
        with pytest.raises(IndexingSubprocessError) as exc_info:
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

        assert elapsed < self.MAX_ALLOWED_SECONDS, (
            f"IndexingSubprocessError raised after {elapsed:.2f}s — the job "
            f"stayed RUNNING until the test poll timeout (expected < {self.MAX_ALLOWED_SECONDS}s)."
        )
        assert "not initialized" in str(exc_info.value).lower() or "initialized" in str(
            exc_info.value
        ), f"Expected 'initialized' in error, got: {exc_info.value!r}"


class TestC1StderrGrandchildHang:
    """C1 regression: stderr reader must not hang when grandchild holds stderr open.

    Before the C1 fix, the finally block never wrote the shutdown byte on the
    natural-stdout-EOF exit path.  If a grandchild held the STDERR write-end
    open after the child exited, the stderr reader stayed parked in
    select.select() until the grandchild died — causing stderr_thread.join()
    to stall the whole call for up to 30s (GIT_COMMAND_TIMEOUT_SECONDS).
    """

    # The grandchild sleeps this long, keeping stderr open.
    GRANDCHILD_SLEEP_SECONDS = 8
    # The call must complete well before the grandchild dies.
    MAX_ALLOWED_SECONDS = 4.0

    def test_stderr_grandchild_hang_c1(self):
        """run_with_popen_progress must return promptly when:
        - the child's STDOUT reaches EOF first (child prints one progress line
          then exits cleanly), AND
        - a grandchild spawned by the child holds the STDERR write-end open
          (close_fds=False) and sleeps for GRANDCHILD_SLEEP_SECONDS.

        Without the C1 fix the call hangs ~8s (until the grandchild dies).
        With the C1 fix the finally block writes the shutdown byte before
        closing the pipe fds, waking the stderr reader immediately.
        """
        import sys
        import time

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        # Child:
        # 1. Prints one progress JSON line to stdout (stdout EOF comes quickly).
        # 2. Spawns a grandchild that inherits all fds (close_fds=False) and
        #    sleeps for GRANDCHILD_SLEEP_SECONDS — keeping stderr write-end open.
        # 3. Exits 0 immediately.
        # Without C1 fix: stderr_thread.join(30s) stalls ~8s.
        # With C1 fix: finally writes shutdown byte -> stderr reader wakes -> done.
        script = (
            "import json, subprocess, sys\n"
            'print(json.dumps({"current": 1, "total": 1, "info": "done"}), flush=True)\n'
            f"subprocess.Popen(['sleep', '{self.GRANDCHILD_SLEEP_SECONDS}'], close_fds=False)\n"
            "sys.exit(0)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

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

        assert elapsed < self.MAX_ALLOWED_SECONDS, (
            f"run_with_popen_progress took {elapsed:.2f}s — C1 stderr-grandchild hang "
            f"is still present (expected < {self.MAX_ALLOWED_SECONDS}s). "
            f"The finally block must write the shutdown byte before closing fds."
        )


class TestC2OutputIntegrityUnderWedge:
    """C2 regression: no output must be dropped when shutdown and data fd are
    simultaneously ready in the same select() cycle.

    Before the C2 fix, both reader loops checked shutdown_r BEFORE the data fd.
    When select() returned both ready at the same time (the common case after
    the C1 fix makes shutdown arrive just as the last bytes land), the reader
    broke immediately and discarded the unread bytes — losing the final
    progress-JSON line (stdout) or the critical error message (stderr).
    """

    # Grandchild keeps pipes open long enough to exercise the wedge path.
    GRANDCHILD_SLEEP_SECONDS = 8

    def test_output_integrity_under_wedge_c2(self):
        """Final stdout progress line AND full stderr error text must survive
        the simultaneous-ready window.

        Scenario: the child writes a final progress-JSON line to stdout AND a
        multi-line error message to stderr, then spawns a grandchild that holds
        both pipes open (close_fds=False) and exits non-zero.  After the C1 fix
        the shutdown byte arrives quickly — potentially in the same select()
        cycle as the last unread bytes.  The C2 fix ensures data is drained
        before the shutdown is honoured, so nothing is dropped.

        Assertions:
        - The progress callback receives the final progress line (stdout not dropped).
        - The raised IndexingSubprocessError contains the full stderr error text
          (stderr not dropped).
        """
        import sys

        import pytest

        from code_indexer.services.progress_phase_allocator import (
            ProgressPhaseAllocator,
        )
        from code_indexer.services.progress_subprocess_runner import (
            IndexingSubprocessError,
            run_with_popen_progress,
        )

        allocator = ProgressPhaseAllocator()
        allocator.calculate_weights(
            index_types=["semantic"],
            file_count=10,
            commit_count=0,
        )

        received_pcts: list = []

        def cb(pct, phase=None, detail=None):
            received_pcts.append(pct)

        # Child:
        # 1. Writes a final progress JSON line to stdout (current==total -> 100% of phase).
        # 2. Writes a distinctive two-line error to stderr (two separate write calls
        #    to avoid embedding a real newline inside the f-string interpolation,
        #    which would produce a broken string literal in the generated script).
        # 3. Spawns a grandchild holding both pipes open (close_fds=False).
        # 4. Exits non-zero — so IndexingSubprocessError must be raised with stderr text.
        script = (
            "import json, subprocess, sys\n"
            'sys.stdout.write(json.dumps({"current": 1, "total": 1, "info": "final"}) + "\\n")\n'
            "sys.stdout.flush()\n"
            'sys.stderr.write("DISTINCTIVE_ERROR_LINE_ONE\\n")\n'
            'sys.stderr.write("DISTINCTIVE_ERROR_LINE_TWO\\n")\n'
            "sys.stderr.flush()\n"
            f"subprocess.Popen(['sleep', '{self.GRANDCHILD_SLEEP_SECONDS}'], close_fds=False)\n"
            "sys.exit(2)\n"
        )
        command = [sys.executable, "-c", script]
        all_stdout: list = []
        all_stderr: list = []

        with pytest.raises(IndexingSubprocessError) as exc_info:
            run_with_popen_progress(
                command=command,
                phase_name="semantic",
                allocator=allocator,
                progress_callback=cb,
                all_stdout=all_stdout,
                all_stderr=all_stderr,
                cwd=None,
            )

        error_msg = str(exc_info.value)

        # C2 stdout check: callback must have received the final progress line.
        # Phase start (0) + at least the final mapped value (phase_end for semantic).
        assert len(received_pcts) >= 2, (
            f"C2: final stdout progress line was dropped — callback received "
            f"only {len(received_pcts)} value(s): {received_pcts}. "
            f"Expected at least phase_start + final-line callback."
        )

        # C2 stderr check: both distinctive lines must appear in the error.
        assert "DISTINCTIVE_ERROR_LINE_ONE" in error_msg, (
            f"C2: first stderr line was dropped from IndexingSubprocessError. "
            f"Got: {error_msg!r}"
        )
        assert "DISTINCTIVE_ERROR_LINE_TWO" in error_msg, (
            f"C2: second stderr line was dropped from IndexingSubprocessError. "
            f"Got: {error_msg!r}"
        )


class TestGatherRepoMetrics:
    """Tests for gather_repo_metrics shared utility."""

    def test_non_git_directory_returns_zero_zero(self):
        """
        For a non-git directory, gather_repo_metrics must return (0, 0)
        gracefully without raising.
        """
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            file_count, commit_count = gather_repo_metrics(tmpdir)

        assert file_count == 0
        assert commit_count == 0

    def test_real_git_repo_returns_positive_counts(self):
        """
        For a real git repository with at least one commit and file,
        gather_repo_metrics must return positive file and commit counts.
        """
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
        )

        # Use the code-indexer repo itself (it has many files and commits)
        project_root = Path(__file__).parent.parent.parent.parent
        if not (project_root / ".git").exists():
            pytest.skip("Not running inside a git repository")

        file_count, commit_count = gather_repo_metrics(str(project_root))

        assert file_count > 0, f"Expected positive file count, got {file_count}"
        assert commit_count > 0, f"Expected positive commit count, got {commit_count}"

    def test_returns_tuple_of_ints(self):
        """
        gather_repo_metrics must always return a tuple of two integers.
        """
        from code_indexer.services.progress_subprocess_runner import (
            gather_repo_metrics,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = gather_repo_metrics(tmpdir)

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], int)
        assert isinstance(result[1], int)
