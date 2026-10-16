"""Process-group termination shared by the CLI/services and server layers.

Layer-neutral (stdlib only) so both `services/` (the indexing subprocess
runner and its activity watchdog) and `server/utils/cancellable_subprocess.py`
use ONE implementation -- previously two hand-copied versions existed.
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
    """True while any process (zombies included) is still in group `pgid`."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def terminate_process_group(
    proc: "subprocess.Popen", grace_seconds: float = SIGTERM_GRACE_SECONDS
) -> None:
    """SIGTERM the child's process group, keep the WHOLE group under
    observation for `grace_seconds`, then SIGKILL every survivor. Always
    blocks until the direct child is reaped.

    Never signals the caller's own process group (see the guard below).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        proc.wait()
        return

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
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait()
        return

    # The direct child exiting is not enough: a grandchild in the same group
    # may ignore SIGTERM, so the GROUP is watched until it is empty or the
    # grace period ends. No blocking wait exists for a whole process group,
    # hence a bounded poll (at most grace_seconds / interval iterations).
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        proc.poll()  # reap the direct child so it no longer counts
        if not _group_has_members(pgid):
            proc.wait()
            return
        time.sleep(_GROUP_POLL_INTERVAL_SECONDS)

    # Grace period over: SIGKILL every member still in the group.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
