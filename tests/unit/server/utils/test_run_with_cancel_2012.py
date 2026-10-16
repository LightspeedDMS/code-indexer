"""Bug #2012 review: every subprocess a refresh starts must be cancellable.

`run_with_cancel` is a drop-in for `subprocess.run` used at those call
sites: with no cancel check it IS `subprocess.run` (unchanged behaviour);
with one, the child runs in its own process group and is terminated when
the job is cancelled. Real child processes only.
"""

import subprocess
import sys
import time
from typing import List

import pytest

from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
    run_with_cancel,
)

_SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def test_without_cancel_check_matches_subprocess_run() -> None:
    result = run_with_cancel(
        [sys.executable, "-c", "print('out')"], None, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout == "out\n"


def test_without_cancel_check_check_true_raises() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_with_cancel(
            [sys.executable, "-c", "raise SystemExit(3)"],
            None,
            capture_output=True,
            text=True,
            check=True,
        )


def test_cancel_terminates_child_and_raises() -> None:
    started = time.monotonic()
    with pytest.raises(SubprocessCancelledError):
        run_with_cancel(
            _SLEEPER,
            lambda: time.monotonic() - started > 0.5,
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert time.monotonic() - started < 10


def test_armed_but_not_cancelled_keeps_run_semantics() -> None:
    polls: List[int] = []

    def never() -> bool:
        polls.append(1)
        return False

    result = run_with_cancel(
        [sys.executable, "-c", "import time; time.sleep(2.5); print('done')"],
        never,
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stdout) == (0, "done\n")
    assert polls, "the cancel check must be polled while the child runs"

    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_with_cancel(
            [sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(4)"],
            never,
            capture_output=True,
            text=True,
            check=True,
        )
    assert raised.value.returncode == 4 and raised.value.stderr == "bad"

    with pytest.raises(subprocess.TimeoutExpired):
        run_with_cancel(_SLEEPER, never, capture_output=True, text=True, timeout=1)


def test_cancel_check_raising_terminates_child(tmp_path) -> None:
    """A cancel check that itself fails must never leave the child running."""
    import psutil

    pid_file = tmp_path / "pid"
    script = (
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )

    def broken() -> bool:
        if pid_file.exists() and pid_file.read_text():
            raise RuntimeError("cancel flag unreadable")
        return False

    with pytest.raises(RuntimeError, match="cancel flag unreadable"):
        run_with_cancel(
            [sys.executable, "-c", script], broken, capture_output=True, text=True
        )
    child = int(pid_file.read_text())
    try:
        assert not psutil.pid_exists(child) or (
            psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        ), "the child survived a failing cancel check"
    finally:
        if psutil.pid_exists(child):
            psutil.Process(child).kill()


def test_armed_timeout_carries_partial_output() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_with_cancel(
            [
                sys.executable,
                "-c",
                "import sys, time; print('partial', flush=True); "
                "sys.stderr.write('err'); sys.stderr.flush(); time.sleep(120)",
            ],
            lambda: False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    assert raised.value.output == "partial\n"
    assert raised.value.stderr == "err"


def test_armed_rejects_unsupported_run_arguments() -> None:
    with pytest.raises(TypeError):
        run_with_cancel(
            ["true"], lambda: False, input="x", capture_output=True, text=True
        )
