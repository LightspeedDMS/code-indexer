"""A child armed with exit_with_parent() never outlives the process that
started it: real processes, a real SIGKILL, no doubles of the kernel."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from code_indexer.utils import parent_death

SRC_DIR = str(Path(__file__).resolve().parents[3] / "src")
EXIT_BOUND_SECONDS = 10.0
GO_FILE_WAIT_SECONDS = 30
GO_FILE_POLL_SECONDS = 0.02
LIVENESS_POLL_SECONDS = 0.05
ALIVE_OBSERVATION_SECONDS = 1.0
# ^ invariant OUTLIVE_SECONDS > every bound above; only the parent-death signal ends the child in time
OUTLIVE_SECONDS = 600

# ^ invariant argv = <expected parent pid> <go file>; go file "" => arm at once
_CHILD = textwrap.dedent(
    f"""
    import sys, time
    from pathlib import Path
    from code_indexer.utils.parent_death import exit_with_parent

    expected, go_file = int(sys.argv[1]), sys.argv[2]
    deadline = time.monotonic() + {GO_FILE_WAIT_SECONDS}
    while go_file and not Path(go_file).exists() and time.monotonic() < deadline:
        time.sleep({GO_FILE_POLL_SECONDS})
    exit_with_parent(expected)
    print("armed", flush=True)
    time.sleep({OUTLIVE_SECONDS})
    """
)

# ^ invariant argv = <child.py> <go file>; child's expected parent = this pid, which lives until killed
_MIDDLE = textwrap.dedent(
    f"""
    import os, subprocess, sys, time

    child_script, go_file = sys.argv[1], sys.argv[2]
    child = subprocess.Popen(
        [sys.executable, child_script, str(os.getpid()), go_file],
        stdout=subprocess.DEVNULL if go_file else subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    print(child.pid, flush=True)
    if not go_file:
        assert child.stdout.readline().strip() == "armed"
        print("ready", flush=True)
    time.sleep({OUTLIVE_SECONDS})
    """
)


@pytest.fixture
def scripts(tmp_path) -> dict:
    child, middle = tmp_path / "child.py", tmp_path / "middle.py"
    child.write_text(_CHILD)
    middle.write_text(_MIDDLE)
    return {"child": str(child), "middle": str(middle)}


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": SRC_DIR}


def _readline(proc: subprocess.Popen) -> str:
    assert proc.stdout is not None
    return str(proc.stdout.readline()).strip()


def _process_gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    # ^ external a dead child reparented to a subreaper may linger as a zombie (state Z)
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def _wait_gone(pid: int, bound: float) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        if _process_gone(pid):
            return True
        time.sleep(LIVENESS_POLL_SECONDS)
    return _process_gone(pid)


def _kill_quietly(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _start_middle(scripts: dict, go_file: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, scripts["middle"], scripts["child"], go_file],
        stdout=subprocess.PIPE,
        text=True,
        env=_env(),
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_child_exits_when_parent_is_sigkilled(scripts):
    middle = _start_middle(scripts, "")
    child_pid = int(_readline(middle))
    try:
        assert _readline(middle) == "ready"
        middle.kill()
        middle.wait(timeout=EXIT_BOUND_SECONDS)
        assert _wait_gone(child_pid, EXIT_BOUND_SECONDS), (
            "armed child survived the SIGKILL of its parent"
        )
    finally:
        _kill_quietly(child_pid)
        middle.kill()
        middle.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_child_exits_when_parent_died_before_arming(scripts, tmp_path):
    go_file = tmp_path / "go"
    middle = _start_middle(scripts, str(go_file))
    child_pid = int(_readline(middle))
    try:
        middle.kill()
        middle.wait(timeout=EXIT_BOUND_SECONDS)
        assert not _process_gone(child_pid), "child must still be waiting to arm"
        go_file.write_text("go")
        assert _wait_gone(child_pid, EXIT_BOUND_SECONDS), (
            "child armed after its parent died kept running"
        )
    finally:
        _kill_quietly(child_pid)
        middle.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_child_keeps_running_while_parent_lives(scripts):
    child = subprocess.Popen(
        [sys.executable, scripts["child"], str(os.getpid()), ""],
        stdout=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    try:
        assert _readline(child) == "armed"
        time.sleep(ALIVE_OBSERVATION_SECONDS)
        assert child.poll() is None, "armed child exited although its parent lives"
    finally:
        child.terminate()
        assert child.wait(timeout=EXIT_BOUND_SECONDS) == -signal.SIGTERM


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_expected_parent_pid_1_is_not_armed():
    probe = (
        "from code_indexer.utils.parent_death import exit_with_parent\n"
        "exit_with_parent(1)\n"
        "print('not-armed', flush=True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=EXIT_BOUND_SECONDS,
    )
    assert (result.returncode, result.stdout.strip()) == (0, "not-armed"), result.stderr


def test_non_positive_parent_pid_is_refused():
    with pytest.raises(ValueError):
        parent_death.exit_with_parent(0)


def test_non_linux_platform_is_refused(monkeypatch):
    monkeypatch.setattr(parent_death.sys, "platform", "darwin")
    with pytest.raises(parent_death.ParentDeathSignalUnsupported):
        parent_death.exit_with_parent(os.getppid())
