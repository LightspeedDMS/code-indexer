"""Stall detection for the backup's network git calls.

The catalog backup holds the exclusive catalog lock across its git
sequence, so a remote that accepts and then stalls must not block it
forever. There is deliberately no wall-clock timeout (a large push may
legitimately take long): a transfer is abandoned only when it stops making
progress -- below HTTP_LOW_SPEED_LIMIT_BYTES per second for
HTTP_LOW_SPEED_TIME_SECONDS over HTTP(S), or SSH_SERVER_ALIVE_COUNT_MAX
unanswered keepalives SSH_SERVER_ALIVE_INTERVAL_SECONDS apart over SSH.
git then fails, which the sync and bootstrap report as a failure.
"""

from __future__ import annotations

from typing import Dict, List

HTTP_LOW_SPEED_LIMIT_BYTES = 1000
HTTP_LOW_SPEED_TIME_SECONDS = 60
SSH_SERVER_ALIVE_INTERVAL_SECONDS = 15
SSH_SERVER_ALIVE_COUNT_MAX = 4


def stall_detecting_git_argv(*args: str) -> List[str]:
    """``git <args>`` with HTTP(S) low-speed abort configured."""
    return [
        "git",
        "-c",
        f"http.lowSpeedLimit={HTTP_LOW_SPEED_LIMIT_BYTES}",
        "-c",
        f"http.lowSpeedTime={HTTP_LOW_SPEED_TIME_SECONDS}",
        *args,
    ]


def with_ssh_keepalive(env: Dict[str, str]) -> Dict[str, str]:
    """*env* with SSH keepalives appended to its ssh command (never
    replacing the options already there)."""
    base = env.get("GIT_SSH_COMMAND") or "ssh"
    env["GIT_SSH_COMMAND"] = (
        f"{base} -o ServerAliveInterval={SSH_SERVER_ALIVE_INTERVAL_SECONDS}"
        f" -o ServerAliveCountMax={SSH_SERVER_ALIVE_COUNT_MAX}"
    )
    return env


# ^ coupled curl's low-speed abort and ssh's unanswered keepalives print these
_STALL_MARKERS = ("Operation too slow", "not responding")
_REJECTION_MARKERS = (
    "rejected",
    "declined",
    "denied",
    "Authentication failed",
    "returned error: 403",
)
_UNREACHABLE_MARKERS = (
    "does not appear to be a git repository",
    "Could not resolve",
    "Connection refused",
    "Connection timed out",
    "No route to host",
    "Network is unreachable",
    "unable to access",
    "Could not read from remote repository",
)


def push_failure_reason(git_output: str) -> str:
    """A neutral reason for a failed push, from git's output: never the raw
    output (it can carry the remote URL). Stall is checked first because a
    stalled transfer's output also says "unable to access"."""
    for markers, reason in (
        (_STALL_MARKERS, "the remote stalled"),
        (_REJECTION_MARKERS, "the remote rejected the push"),
        (_UNREACHABLE_MARKERS, "the remote is unreachable"),
    ):
        if any(marker in git_output for marker in markers):
            return reason
    return "the push failed"
