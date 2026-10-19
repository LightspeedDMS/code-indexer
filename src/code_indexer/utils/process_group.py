"""Process-group termination shared by the CLI/services and server layers.

Layer-neutral (stdlib only) so every caller (the indexing subprocess runner
and its activity watchdog, ``server/utils/cancellable_subprocess.py``, the
X-Ray backend, the Codex invoker, the job manager's cancel) uses ONE
implementation.
"""

import logging
import os
import signal
import subprocess
import time

logger = logging.getLogger(__name__)

#: Default grace period after SIGTERM before survivors are SIGKILLed.
SIGTERM_GRACE_SECONDS = 2.0

#: How often the group is re-checked for survivors during the grace period.
_GROUP_POLL_INTERVAL_SECONDS = 0.05


def _group_has_members(pgid: int) -> bool:
    """True while any process (zombies included) is still in group `pgid`.

    Raises PermissionError when the group holds processes this one may not
    signal.
    """
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def _reap_child_alone(proc: "subprocess.Popen", grace_seconds: float) -> None:
    """SIGKILL and reap the direct child only, waiting at most grace_seconds."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except PermissionError:
        logger.warning(
            "terminate_process_group: not permitted to signal pid=%s", proc.pid
        )
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        logger.warning("terminate_process_group: pid=%s not reaped in time", proc.pid)


def _not_terminated(proc: "subprocess.Popen", pgid: int, grace_seconds: float) -> bool:
    """The group could not be signalled: report it, never escalate further."""
    logger.warning(
        "terminate_process_group: not permitted to signal process group %s of "
        "pid=%s; the group is NOT reported terminated",
        pgid,
        proc.pid,
    )
    _reap_child_alone(proc, grace_seconds)
    return False


def terminate_process_group(
    proc: "subprocess.Popen", grace_seconds: float = SIGTERM_GRACE_SECONDS
) -> bool:
    """SIGTERM the child's process group, keep the WHOLE group under
    observation for `grace_seconds`, then SIGKILL every survivor.

    Returns True once the group is gone (the direct child reaped). Returns
    False when the group could not be signalled (EPERM): nothing further is
    sent to it, the direct child alone is killed and reaped (bounded), and
    the caller decides what that means. Never signals the caller's own
    process group (see the guard below).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        proc.wait()
        return True

    if pgid == os.getpgrp():
        # A child not started in its own session shares OUR group: killpg
        # would terminate the server itself. Stop that child alone.
        logger.error(
            "terminate_process_group: pid=%s shares the caller's own process "
            "group %s (not started in its own session); terminating that "
            "process only, never the group",
            proc.pid,
            pgid,
        )
        proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return True

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return True
    except PermissionError:
        return _not_terminated(proc, pgid, grace_seconds)

    # The direct child exiting is not enough: a grandchild in the same group
    # may ignore SIGTERM, so the GROUP is watched until it is empty or the
    # grace period ends. No blocking wait exists for a whole process group,
    # hence a bounded poll (at most grace_seconds / interval iterations).
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        proc.poll()  # reap the direct child so it no longer counts
        try:
            if not _group_has_members(pgid):
                proc.wait()
                return True
        except PermissionError:
            return _not_terminated(proc, pgid, grace_seconds)
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)

    # Grace period over: SIGKILL every member still in the group.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        return _not_terminated(proc, pgid, grace_seconds)
    proc.wait()
    return True
