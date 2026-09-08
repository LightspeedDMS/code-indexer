"""R2-3 (Codex re-review): orphan-process fix for `_run_xray_cli_process`.

`subprocess.Popen` was spawned WITHOUT `start_new_session=True`, and the
timeout path called only `proc.kill()` (SIGKILL to the DIRECT child).
SIGKILL cannot be caught or forwarded by the killed process, so any
GRANDCHILD it spawned (e.g. rustc launched by xray-cli) is reparented to
init and survives as an orphan, indefinitely consuming CPU/RAM.

Real subprocess test (no mocks): spawns a bash script that itself forks a
background "grandchild" sleep process and writes its PID to a file, then
sleeps past the configured timeout itself. Asserts the grandchild does
NOT survive the timeout-triggered kill.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path
from unittest.mock import patch

from code_indexer.xray.rust_backend import RustNativeBackend

# Named timing constants (all bounded -- never open-ended waits).
_GRANDCHILD_SLEEP_SECONDS = 30  # far longer than the timeout under test
_PARENT_SLEEP_SECONDS = 30  # ditto -- must still be "running" at timeout
_TIMEOUT_SECONDS = 1  # deliberately short so the test finishes quickly
_PID_FILE_WAIT_DEADLINE_SECONDS = 5.0  # bound for the pid file to appear
_PID_FILE_POLL_INTERVAL_SECONDS = 0.05
_REAP_WAIT_DEADLINE_SECONDS = 3.0  # bound for the grandchild to die
_REAP_POLL_INTERVAL_SECONDS = 0.05


def _pid_alive(pid: int) -> bool:
    """True if `pid` is a live, non-zombie process.

    A killed process that has not yet been reaped by its (possibly new,
    post-orphan) parent shows up as a ZOMBIE ('Z' state in
    /proc/<pid>/stat) -- `os.kill(pid, 0)` still succeeds against a
    zombie's PID since the kernel has not recycled it yet, which would
    make a genuinely-killed grandchild look "alive" and produce a false
    test failure. Zombie state is therefore treated as dead here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by someone else -- still "alive" for
        # our purposes (shouldn't normally happen for our own descendant,
        # but fail safe rather than silently treating it as dead).
        return True

    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        # Process vanished between os.kill(0) succeeding and reading
        # /proc -- treat as dead (the race resolved in our favor).
        return False

    # Format: "<pid> (<comm>) <state> ...". <comm> can itself contain
    # spaces/parens, so split on the LAST ')' to isolate the state field.
    after_comm = stat_text.rsplit(")", 1)[-1]
    state = after_comm.split()[0]
    return state != "Z"


def test_orphan_descendant_does_not_survive_timeout_kill(tmp_path: Path) -> None:
    """A grandchild process spawned by the timed-out child must NOT
    survive `_run_xray_cli_process`'s timeout-triggered kill."""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_and_sleep.sh"
    script.write_text(
        "#!/bin/bash\n"
        f"sleep {_GRANDCHILD_SLEEP_SECONDS} &\n"
        f"echo $! > {shlex.quote(str(pid_file))}\n"
        f"sleep {_PARENT_SLEEP_SECONDS}\n"
    )
    script.chmod(0o755)

    backend = RustNativeBackend()
    _stdout, error = backend._run_xray_cli_process(
        [str(script)],
        timeout_seconds=_TIMEOUT_SECONDS,
        on_process_spawned=None,
        acquire_compile_slot=False,
    )

    assert error is not None, "expected a timeout error to be reported"
    assert "timed out" in error.lower()

    pid_file_deadline = time.time() + _PID_FILE_WAIT_DEADLINE_SECONDS
    while not pid_file.exists() and time.time() < pid_file_deadline:
        time.sleep(_PID_FILE_POLL_INTERVAL_SECONDS)
    assert pid_file.exists(), "grandchild pid file was never written"
    grandchild_pid = int(pid_file.read_text().strip())

    reap_deadline = time.time() + _REAP_WAIT_DEADLINE_SECONDS
    grandchild_alive = _pid_alive(grandchild_pid)
    while grandchild_alive and time.time() < reap_deadline:
        time.sleep(_REAP_POLL_INTERVAL_SECONDS)
        grandchild_alive = _pid_alive(grandchild_pid)

    assert not grandchild_alive, (
        f"grandchild process {grandchild_pid} survived the timeout kill -- "
        "orphaned descendants must be reaped via process-group kill "
        "(start_new_session=True + os.killpg on timeout)"
    )


def test_permission_error_from_killpg_does_not_prevent_reaping_or_propagate(
    tmp_path: Path,
) -> None:
    """Polish item (Codex re-review, ROUND 3): `os.killpg` can also raise
    `PermissionError` (a rare but real OS-level race -- the process group
    leader already exited and its PGID was reused by an unrelated process
    this session cannot signal), not just `ProcessLookupError`. Currently
    only `ProcessLookupError` is caught, so `PermissionError` would
    propagate uncaught, skip `proc.wait()`, and leave the direct child
    unreaped. Uses a REAL slow subprocess to trigger a genuine timeout,
    with `os.killpg` patched to raise `PermissionError` (the one specific
    OS call being simulated, since a genuine PGID-reuse race cannot be
    reliably constructed in a test) -- everything else (the real
    subprocess, real `proc.wait()` reaping) stays real. Captures the real
    child PID via `on_process_spawned` and proves it is genuinely reaped
    afterward, not just that a normal error tuple was returned."""
    script = tmp_path / "sleep_only.sh"
    script.write_text(f"#!/bin/bash\nsleep {_PARENT_SLEEP_SECONDS}\n")
    script.chmod(0o755)

    spawned_pid: dict = {}

    def _capture_pid(proc) -> None:  # type: ignore[no-untyped-def]
        spawned_pid["value"] = proc.pid

    backend = RustNativeBackend()
    with patch("os.killpg", side_effect=PermissionError("simulated PGID-reuse race")):
        _stdout, error = backend._run_xray_cli_process(
            [str(script)],
            timeout_seconds=_TIMEOUT_SECONDS,
            on_process_spawned=_capture_pid,
            acquire_compile_slot=False,
        )

    assert error is not None, (
        "a PermissionError from os.killpg must not prevent the normal "
        "timeout error tuple from being returned"
    )
    assert "timed out" in error.lower()

    assert "value" in spawned_pid, "on_process_spawned callback was never invoked"
    assert not _pid_alive(spawned_pid["value"]), (
        f"direct child pid {spawned_pid['value']} must still be reaped "
        "(proc.wait() must run) even when os.killpg itself raised "
        "PermissionError"
    )
