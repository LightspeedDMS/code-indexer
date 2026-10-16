"""Bug #2012 review: process-group termination must stop EVERY member of the
group, and must never signal the caller's own process group.

Real processes only. The own-group test runs inside a separate harness
process that is its own session leader, so a missing guard kills the
harness -- never this pytest process.
"""

import os
import signal
import subprocess
import sys
import time

import psutil

from code_indexer.utils.process_group import terminate_process_group

_BOUND_SECONDS = 15.0
_LONG_SLEEP = 120


def _is_running(pid: int) -> bool:
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


# The child exits on SIGTERM (default action); its grandchild, in the SAME
# process group, ignores SIGTERM and reports readiness before the child does.
_STUBBORN_GRANDCHILD = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    f"print('ready', flush=True); time.sleep({_LONG_SLEEP})"
)
_CHILD_WITH_STUBBORN_GRANDCHILD = (
    "import subprocess, sys\n"
    f"g = subprocess.Popen([sys.executable, '-c', {_STUBBORN_GRANDCHILD!r}], "
    "stdout=subprocess.PIPE, text=True)\n"
    "assert g.stdout.readline().strip() == 'ready'\n"
    "print(g.pid, flush=True)\n"
    f"import time; time.sleep({_LONG_SLEEP})\n"
)


def test_grandchild_ignoring_sigterm_is_killed() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_WITH_STUBBORN_GRANDCHILD],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None
    grandchild_pid = int(proc.stdout.readline().strip())
    try:
        start = time.monotonic()
        terminate_process_group(proc)
        elapsed = time.monotonic() - start

        assert proc.returncode is not None, "the direct child must be reaped"
        assert elapsed < _BOUND_SECONDS
        assert _wait_until_gone(grandchild_pid, 5.0), (
            "a group member that ignores SIGTERM survived termination"
        )
    finally:
        for pid in (proc.pid, grandchild_pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


_OWN_GROUP_HARNESS = (
    "import subprocess, sys\n"
    "from code_indexer.utils.process_group import terminate_process_group\n"
    # Child deliberately started WITHOUT a new session: it shares the
    # harness's own process group.
    f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_LONG_SLEEP})'])\n"
    "terminate_process_group(child)\n"
    "print('harness-survived', child.returncode is not None, flush=True)\n"
)


def test_never_signals_callers_own_process_group() -> None:
    harness = subprocess.run(
        [sys.executable, "-c", _OWN_GROUP_HARNESS],
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert "harness-survived True" in harness.stdout, (
        f"terminate_process_group signalled its caller's own process group "
        f"(rc={harness.returncode}, stderr={harness.stderr[-500:]!r})"
    )
