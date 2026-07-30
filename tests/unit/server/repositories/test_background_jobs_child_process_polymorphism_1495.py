"""
Unit tests for Bug #1495: BackgroundJobManager._terminate_child_processes
must be polymorphic over both multiprocessing.Process and subprocess.Popen.

Two spawn paths register child handles into the SAME tracker via
register_child_process:
- X-Ray sandbox path registers a multiprocessing.Process (has
  .is_alive()/.join()).
- X-Ray Rust dynlib path (xray/rust_backend.py) registers a
  subprocess.Popen, which has .poll()/.wait()/.terminate()/.kill() but
  NO .is_alive()/.join().

Before the fix, _terminate_child_processes called proc.is_alive() /
proc.join(timeout=...) unconditionally, raising AttributeError for a
registered Popen and leaking the process. These tests use REAL process
objects (no mocking of the process handles) to prove genuine termination
behavior, not merely "no exception".
"""

from __future__ import annotations

import multiprocessing
import subprocess
import sys
import time
from unittest.mock import patch

from code_indexer.server.repositories.background_jobs import BackgroundJobManager


def _make_bjm() -> BackgroundJobManager:
    """Create a BackgroundJobManager without maintenance mode or persistence."""
    with patch(
        "code_indexer.server.services.maintenance_service.get_maintenance_state"
    ) as mock_maint:
        mock_maint.return_value.is_maintenance_mode.return_value = False
        bjm = BackgroundJobManager()
    return bjm


def _mp_sleep_worker(seconds: float) -> None:
    """Top-level (picklable) target for a real multiprocessing.Process."""
    time.sleep(seconds)


class TestTerminateChildProcessesPopen1495:
    """Bug #1495: subprocess.Popen handles must terminate without AttributeError."""

    def test_terminate_child_processes_terminates_real_popen_without_attributeerror(
        self,
    ):
        """A real subprocess.Popen registered as a child process must be
        terminated by _terminate_child_processes without raising
        AttributeError, and must actually be dead afterward."""
        bjm = _make_bjm()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            bjm.register_child_process("job-popen-1495", proc)

            # This must NOT raise AttributeError('Popen' object has no
            # attribute 'is_alive') -- that is the exact production crash
            # (log ID 596970) this bug fixes.
            bjm._terminate_child_processes("job-popen-1495")

            # Give the OS a moment to reap the signal, then assert genuinely
            # dead (not merely "no exception was raised").
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert proc.poll() is not None, (
                "Popen child process must be terminated (dead), not merely "
                "survive without an AttributeError"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_terminate_child_processes_still_terminates_real_multiprocessing_process(
        self,
    ):
        """Regression: a real multiprocessing.Process must still be
        terminated correctly (no regression to the pre-existing working
        path introduced by the Popen polymorphism fix)."""
        bjm = _make_bjm()
        proc = multiprocessing.Process(target=_mp_sleep_worker, args=(30,))
        proc.start()
        try:
            bjm.register_child_process("job-mp-1495", proc)

            bjm._terminate_child_processes("job-mp-1495")

            proc.join(timeout=5)
            assert not proc.is_alive(), (
                "multiprocessing.Process child must be terminated (dead) "
                "after _terminate_child_processes"
            )
        finally:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=5)
