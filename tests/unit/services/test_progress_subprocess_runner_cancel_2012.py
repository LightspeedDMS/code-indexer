"""Bug #2012 Part 1: cancelling a job must stop its indexing subprocess.

Every test here drives a REAL child process (``sys.executable -c ...``)
through the real ``run_with_popen_progress`` -- no mocks. The child prints
its own pid and the pid of a grandchild it spawned into the same process
group, so the tests can prove the WHOLE group is gone after a cancel.
"""

import json
import sys
import time
from typing import List

import psutil
import pytest

from code_indexer.services.progress_phase_allocator import ProgressPhaseAllocator
from code_indexer.services.progress_subprocess_runner import (
    IndexingCancelledError,
    run_with_popen_progress,
)

# The child would sleep far longer than any bound asserted below.
_CHILD_SLEEP_SECONDS = 120
# Cancel poll is ~2s, SIGTERM grace ~2s: a cancel must land well inside this.
_CANCEL_BOUND_SECONDS = 8.0

_SLEEPER_WITH_GRANDCHILD = (
    "import json, subprocess, sys, time\n"
    f"g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_CHILD_SLEEP_SECONDS})'])\n"
    "print(json.dumps({'child_pid': __import__('os').getpid(), 'grandchild_pid': g.pid}), flush=True)\n"
    f"time.sleep({_CHILD_SLEEP_SECONDS})\n"
)


def _make_allocator() -> ProgressPhaseAllocator:
    allocator = ProgressPhaseAllocator()
    allocator.calculate_weights(index_types=["semantic"], file_count=1, commit_count=0)
    return allocator


def _is_running(pid: int) -> bool:
    """True when pid is a live, non-zombie process."""
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def _wait_until_gone(pid: int, bound_seconds: float) -> bool:
    deadline = time.monotonic() + bound_seconds
    while time.monotonic() < deadline:
        if not _is_running(pid):
            return True
        time.sleep(0.05)
    return not _is_running(pid)


def _pids_from_stdout(all_stdout: List[str]) -> dict:
    for line in all_stdout:
        line = line.strip()
        if line.startswith("{") and "child_pid" in line:
            pids: dict = json.loads(line)
            return pids
    raise AssertionError(f"child never reported its pids: {all_stdout!r}")


class TestCancelTerminatesSubprocessGroup:
    def test_cancel_kills_child_and_grandchild_within_bound(self) -> None:
        all_stdout: List[str] = []
        armed_at: List[float] = []

        def cancel_check() -> bool:
            # Flip to cancelled once the child has reported its pids.
            if any("child_pid" in line for line in all_stdout):
                if not armed_at:
                    armed_at.append(time.monotonic())
                return True
            return False

        start = time.monotonic()
        with pytest.raises(IndexingCancelledError):
            run_with_popen_progress(
                command=[sys.executable, "-c", _SLEEPER_WITH_GRANDCHILD],
                phase_name="semantic",
                allocator=_make_allocator(),
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=[],
                cwd=None,
                cancel_check=cancel_check,
            )
        elapsed = time.monotonic() - start

        pids = _pids_from_stdout(all_stdout)
        assert armed_at, "cancel_check was never consulted after the child started"
        assert elapsed < _CANCEL_BOUND_SECONDS, (
            f"cancel took {elapsed:.1f}s; the child must be stopped within "
            f"{_CANCEL_BOUND_SECONDS}s of cancel_check returning True"
        )
        assert not _is_running(pids["child_pid"])
        assert _wait_until_gone(pids["grandchild_pid"], 5.0), (
            "grandchild in the child's process group survived the cancel"
        )

    def test_no_cancel_lets_child_finish_normally(self) -> None:
        calls: List[int] = []

        def cancel_check() -> bool:
            calls.append(1)
            return False

        high = run_with_popen_progress(
            command=[
                sys.executable,
                "-c",
                "import time; time.sleep(2.5); "
                'print(\'{"current": 1, "total": 1, "info": "done"}\', flush=True)',
            ],
            phase_name="semantic",
            allocator=_make_allocator(),
            progress_callback=None,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
            cancel_check=cancel_check,
        )
        assert high >= 0
        assert calls, "cancel_check must be polled while the child runs"

    def test_cancel_check_that_raises_is_treated_as_not_cancelled(self, caplog) -> None:
        """An unreadable cancel flag (e.g. a transient DB error) must never
        stop a legitimately long indexing run."""

        def broken_cancel_check() -> bool:
            raise RuntimeError("cancel flag unreadable")

        run_with_popen_progress(
            command=[sys.executable, "-c", "import time; time.sleep(2.5)"],
            phase_name="semantic",
            allocator=_make_allocator(),
            progress_callback=None,
            all_stdout=[],
            all_stderr=[],
            cwd=None,
            cancel_check=broken_cancel_check,
        )
        assert "cancel flag unreadable" in caplog.text

    def test_cancel_already_requested_never_spawns(self, tmp_path) -> None:
        marker = tmp_path / "spawned"
        with pytest.raises(IndexingCancelledError):
            run_with_popen_progress(
                command=[
                    sys.executable,
                    "-c",
                    f"open({str(marker)!r}, 'w').close()",
                ],
                phase_name="semantic",
                allocator=_make_allocator(),
                progress_callback=None,
                all_stdout=[],
                all_stderr=[],
                cwd=None,
                cancel_check=lambda: True,
            )
        assert not marker.exists(), "a cancelled job must not start a new subprocess"

    def test_cancel_seen_after_child_closes_stdout(self) -> None:
        """A child that closes stdout but keeps running must still be
        cancellable -- the loop must not fall into an unbounded wait."""
        script = (
            "import json, os, sys, time\n"
            "print(json.dumps({'child_pid': os.getpid(), 'grandchild_pid': os.getpid()}), flush=True)\n"
            "sys.stdout.close(); os.close(1)\n"
            f"time.sleep({_CHILD_SLEEP_SECONDS})\n"
        )
        all_stdout: List[str] = []
        start_holder: List[float] = []

        def cancel_check() -> bool:
            if not start_holder:
                start_holder.append(time.monotonic())
            return time.monotonic() - start_holder[0] > 1.0

        start = time.monotonic()
        with pytest.raises(IndexingCancelledError):
            run_with_popen_progress(
                command=[sys.executable, "-c", script],
                phase_name="semantic",
                allocator=_make_allocator(),
                progress_callback=None,
                all_stdout=all_stdout,
                all_stderr=[],
                cwd=None,
                cancel_check=cancel_check,
            )
        assert time.monotonic() - start < _CANCEL_BOUND_SECONDS + 2
        assert not _is_running(_pids_from_stdout(all_stdout)["child_pid"])
