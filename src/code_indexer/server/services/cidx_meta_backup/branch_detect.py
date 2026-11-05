"""Default-branch detection for Story #926."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from code_indexer.utils.git_runner import run_git_command


def detect_default_branch(
    cidx_meta_path: str,
    timeout: int = 30,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Return the remote HEAD branch for origin, or None on any failure.

    Bug #2012: with the owning job's ``cancel_check`` the git call is
    terminated on cancel and ``SubprocessCancelledError`` propagates (a
    cancellation is not a detection failure).
    """
    try:
        # Through the git seam chokepoint (EVO-64678), like every other git
        # call cidx-meta backup makes; a read, so it runs locally.
        result = run_git_command(
            ["git", "remote", "show", "origin"],
            cwd=Path(cidx_meta_path),
            cancel_check=cancel_check,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=build_non_interactive_git_env(),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("HEAD branch:"):
            branch = stripped.split(":", 1)[1].strip()
            return branch or None
    return None
