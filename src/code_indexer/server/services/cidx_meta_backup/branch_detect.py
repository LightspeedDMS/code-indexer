"""Default-branch detection for Story #926."""

from __future__ import annotations

import subprocess
from typing import Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env


def detect_default_branch(cidx_meta_path: str, timeout: int = 30) -> Optional[str]:
    """Return the remote HEAD branch for origin, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "remote", "show", "origin"],
            cwd=cidx_meta_path,
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
