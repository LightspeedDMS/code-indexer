"""Make a process exit when the process that started it dies (Linux)."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

#: The internal `cidx index` option the server stamps on every child it
#: spawns (single ``--opt=value`` token, see exit_with_parent_arg).
EXIT_WITH_PARENT_OPTION = "--exit-with-parent"

_PR_SET_PDEATHSIG = 1


class ParentDeathSignalUnsupported(RuntimeError):
    pass


def exit_with_parent_arg(parent_pid: int) -> str:
    return f"{EXIT_WITH_PARENT_OPTION}={parent_pid}"


def exit_with_parent(expected_parent_pid: int) -> None:
    """Arm SIGTERM for this process when its parent dies; if the parent is
    already gone (it is not ``expected_parent_pid`` any more), SIGTERM
    this process now. An expected parent of PID 1 arms nothing. Raises on a
    non-Linux platform, a non-positive pid or a failed prctl."""
    if not sys.platform.startswith("linux"):
        raise ParentDeathSignalUnsupported(
            f"exit-with-parent needs Linux prctl(PR_SET_PDEATHSIG); "
            f"unsupported on {sys.platform}"
        )
    if expected_parent_pid <= 0:
        raise ValueError(f"expected parent pid must be > 0, got {expected_parent_pid}")
    # ^ why skip arming for PID 1, not refuse; its death ends the pid namespace and every child in it (container entrypoint)
    if expected_parent_pid == 1:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    # ^ external the signal fires when the parent THREAD that forked us exits, not only its process
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_PDEATHSIG) failed: {os.strerror(errno)}")
    # ^ order prctl before the getppid check; a parent dead before prctl => no signal ever fires
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGTERM)
