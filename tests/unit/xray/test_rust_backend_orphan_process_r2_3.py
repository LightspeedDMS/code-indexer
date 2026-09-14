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


def _pid_state(pid: int) -> str:
    """Classify `pid` as "gone" (fully reaped -- no process-table entry
    at all), "zombie" (exited but not yet reaped by its parent), or
    "running" (still executing).

    Unlike `_pid_alive` above (which deliberately treats a zombie as
    dead -- correct for the sibling orphan-grandchild test, where a
    killed orphan grandchild is expected to end up a zombie under its
    new (init) parent), this function must NOT collapse "zombie" into
    "gone": `os.kill(pid, 0)` succeeds against a zombie's pid because
    the kernel has not recycled the process-table entry yet, so a bug
    that merely SIGKILLs the child but skips `proc.wait()` -- leaving
    it an unreaped zombie -- must be reported as "zombie", not "gone".
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        # Process exists but owned by someone else -- shouldn't happen
        # for our own child, but fail safe rather than claim "gone".
        return "running"

    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        # Vanished between os.kill(0) succeeding and reading /proc --
        # the race resolved in our favor.
        return "gone"

    after_comm = stat_text.rsplit(")", 1)[-1]
    state = after_comm.split()[0]
    return "zombie" if state == "Z" else "running"


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
    # This test mocks os.killpg to RAISE, so the child is never actually
    # signalled -- it exits only when its own sleep ends, and proc.wait()
    # blocks for that whole duration. Reusing _PARENT_SLEEP_SECONDS (30) made
    # this the slowest test in tests/unit/xray/ at 30.08s, past the fast
    # suite's 10s investigate threshold. It only has to still be RUNNING when
    # the 1s timeout fires, so a few seconds preserves the exact condition
    # under test at a fraction of the cost.
    unkilled_parent_sleep_seconds = 3
    script = tmp_path / "sleep_only.sh"
    script.write_text(f"#!/bin/bash\nsleep {unkilled_parent_sleep_seconds}\n")
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

    # NOTE: deliberately uses `_pid_state` (gone/zombie/running), NOT the
    # zombie-tolerant `_pid_alive` above. `_pid_alive` treats a zombie as
    # dead, which would make this assertion pass whether or not
    # `proc.wait()` actually ran -- a bug that SIGKILLs the child but skips
    # `proc.wait()` leaves it an unreaped ZOMBIE, and `os.kill(pid, 0)`
    # alone cannot tell that apart from a fully-reaped pid (Bug #1819).
    reap_deadline = time.time() + _REAP_WAIT_DEADLINE_SECONDS
    child_state = _pid_state(spawned_pid["value"])
    while child_state != "gone" and time.time() < reap_deadline:
        time.sleep(_REAP_POLL_INTERVAL_SECONDS)
        child_state = _pid_state(spawned_pid["value"])

    assert child_state == "gone", (
        f"direct child pid {spawned_pid['value']} must be fully reaped "
        "(proc.wait() must run) even when os.killpg itself raised "
        f"PermissionError -- observed state: {child_state!r} (a 'zombie' "
        "state means the process exited but proc.wait() never consumed "
        "it, which os.kill(pid, 0)/`_pid_alive` alone cannot detect since "
        "both also report success/alive against a zombie's still-present "
        "process-table entry)"
    )
