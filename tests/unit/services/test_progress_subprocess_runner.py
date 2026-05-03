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
