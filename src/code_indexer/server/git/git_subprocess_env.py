"""
Shared environment builder for git subprocess calls.

Ensures SSH never prompts for interactive passwords, preventing server worker
threads from hanging indefinitely when key authentication fails against an
SSH remote (e.g. git@gitlab.com or git@github.com).

Every git clone/fetch/pull/push/ls-remote call that may contact an SSH remote
MUST pass env=build_non_interactive_git_env() to subprocess.run / subprocess.Popen.

See Bug: SSH password prompt hangs server thread.
"""

import os
from typing import Dict


def build_non_interactive_git_env() -> Dict[str, str]:
    """Return a copy of the current environment augmented for non-interactive git SSH.

    The returned dict:
    - Inherits all variables from os.environ (PATH, HOME, SSH_AUTH_SOCK, etc.)
    - Sets GIT_SSH_COMMAND with BatchMode=yes and fail-fast SSH options so that
      SSH exits immediately with an error instead of prompting for a password or
      blocking on a tty when key authentication fails.
    - Sets GIT_TERMINAL_PROMPT=0 to disable git's own HTTP credential prompt.

    Callers receive a fresh dict each time; os.environ is never mutated.
    """
    env: Dict[str, str] = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        "ssh"
        " -o BatchMode=yes"
        " -o ConnectTimeout=10"
        " -o StrictHostKeyChecking=accept-new"
        " -o PasswordAuthentication=no"
        " -o KbdInteractiveAuthentication=no"
        " -o PubkeyAuthentication=yes"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
