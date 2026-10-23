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
