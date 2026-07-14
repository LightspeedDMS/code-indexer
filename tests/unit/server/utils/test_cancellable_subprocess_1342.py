"""
Unit tests for the cancellable-subprocess engine (Bug #1342).

Cancelling a running activation job is a no-op while the worker is blocked
inside a long subprocess (CoW clone / `cidx index` branch-delta reindex).
`run_cancellable_subprocess` is the shared engine that makes ANY subprocess
call cooperatively cancellable: it spawns the child in its own process
GROUP (start_new_session=True) and polls with a short timeout, checking an
injected `cancel_check()` callable on each timeout; on cancellation it kills
the whole process group (SIGTERM, brief grace, SIGKILL) and raises
SubprocessCancelledError.

Mocking policy (per Bug #1342 task): NO process mocks. Every test here
spawns a REAL child process (bash) and proves real OS-level behavior:
process (and its process-group children) actually die, real stdout/stderr
are captured, and — critically — the poll loop never imposes an artificial
wall-clock ceiling of its own (Bug #1218 guard).
"""

import os
import subprocess
import time

import pytest

from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
    run_cancellable_subprocess,
)


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this pid is still alive (POSIX)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — still alive for our purposes.
        return True
    return True


class TestRunCancellableSubprocessSuccessPath:
    """No cancellation requested: behaves like a normal, uninterrupted run."""

    def test_captures_stdout_and_zero_returncode_on_success(self):
        result = run_cancellable_subprocess(
            ["bash", "-c", "echo hello-1342"],
            cwd="/tmp",
            cancel_check=None,
        )
        assert result.returncode == 0
        assert "hello-1342" in result.stdout

    def test_captures_nonzero_returncode_without_raising(self):
        result = run_cancellable_subprocess(
            ["bash", "-c", "exit 7"],
            cwd="/tmp",
            cancel_check=None,
        )
        assert result.returncode == 7

    def test_cancel_check_returning_false_never_kills_process(self):
        """Bug #1218 guard: a cancel_check that never fires must not
        interrupt a subprocess that legitimately runs longer than several
        poll intervals — there is NO implicit wall-clock ceiling."""
        calls = {"count": 0}

        def never_cancel() -> bool:
            calls["count"] += 1
            return False

        result = run_cancellable_subprocess(
            ["bash", "-c", "sleep 0.3; echo done"],
            cwd="/tmp",
            cancel_check=never_cancel,
            poll_interval=0.05,
        )
        assert result.returncode == 0
        assert "done" in result.stdout
        # Proves the poll loop actually iterated multiple times (real
        # polling happened) rather than a single blocking wait.
        assert calls["count"] >= 3


class TestRunCancellableSubprocessCancellation:
    """cancel_check() returning True must kill the REAL process group promptly."""

    def test_cancel_raises_subprocess_cancelled_error(self):
        with pytest.raises(SubprocessCancelledError):
            run_cancellable_subprocess(
                ["bash", "-c", "sleep 30"],
                cwd="/tmp",
                cancel_check=lambda: True,
                poll_interval=0.05,
            )

    def test_cancel_kills_process_within_a_few_seconds(self):
        """The whole point of Bug #1342: cancel must be prompt, not wait
        out a 30s sleep."""
        start = time.monotonic()
        with pytest.raises(SubprocessCancelledError):
            run_cancellable_subprocess(
                ["bash", "-c", "sleep 30"],
                cwd="/tmp",
                cancel_check=lambda: True,
                poll_interval=0.05,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s, expected < 5s"

    def test_cancel_kills_entire_process_group_not_just_direct_child(self, tmp_path):
        """Proves process-GROUP kill: a grandchild spawned by the direct
        bash child must also die, not just the immediate pid."""
        pid_file = tmp_path / "grandchild.pid"
        # The direct child is bash; it backgrounds a grandchild `sleep 30`
        # and writes the grandchild's real pid to disk, then waits on it.
        script = f"sleep 30 & echo $! > {pid_file}; wait"

        cancelled = {"fired": False}

        def cancel_once_pidfile_exists():
            if cancelled["fired"]:
                return True
            if pid_file.exists():
                cancelled["fired"] = True
                return True
            return False

        with pytest.raises(SubprocessCancelledError):
            run_cancellable_subprocess(
                ["bash", "-c", script],
                cwd=str(tmp_path),
                cancel_check=cancel_once_pidfile_exists,
                poll_interval=0.02,
            )

        grandchild_pid = int(pid_file.read_text().strip())
        assert not _pid_alive(grandchild_pid), (
            "grandchild `sleep 30` survived process-group kill"
        )


class TestRunCancellableSubprocessOptionalTimeout:
    """Bug #1285 preservation: callers (CoW clone) may still enforce their
    own overall wall-clock deadline via the `timeout` kwarg, independent of
    cancel_check."""

    def test_timeout_kills_process_and_raises_timeout_expired(self):
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_cancellable_subprocess(
                ["bash", "-c", "sleep 30"],
                cwd="/tmp",
                cancel_check=None,
                poll_interval=0.05,
                timeout=0.3,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"timeout enforcement took {elapsed:.2f}s"

    def test_no_timeout_means_no_wall_clock_ceiling(self):
        """Bug #1218: timeout=None (the indexing-path default) must allow a
        subprocess to run past several poll intervals without being killed."""
        result = run_cancellable_subprocess(
            ["bash", "-c", "sleep 0.2; echo survived"],
            cwd="/tmp",
            cancel_check=None,
            poll_interval=0.02,
            timeout=None,
        )
        assert result.returncode == 0
        assert "survived" in result.stdout


class TestTerminateProcessGroupHelper:
    """Direct coverage of the SIGTERM-then-SIGKILL escalation path."""

    def test_ignores_sigterm_process_escalates_to_sigkill(self, tmp_path):
        """A process that traps/ignores SIGTERM must still die via the
        SIGKILL escalation within the grace-period bound."""
        pid_file = tmp_path / "trap.pid"
        script = f"trap '' TERM; echo $$ > {pid_file}; sleep 30"

        start = time.monotonic()
        with pytest.raises(SubprocessCancelledError):
            run_cancellable_subprocess(
                ["bash", "-c", script],
                cwd=str(tmp_path),
                cancel_check=lambda: True,
                poll_interval=0.02,
            )
        elapsed = time.monotonic() - start
        # SIGTERM grace period is short (a couple seconds); SIGKILL always
        # succeeds against a process even if it traps SIGTERM.
        assert elapsed < 8.0, f"SIGKILL escalation took {elapsed:.2f}s"
