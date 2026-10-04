"""A throwaway global git config for a test (Bug #2028).

``DeploymentExecutor.execute()`` runs the real ``git config --global``
safe.directory self-heal, so any test reaching it unpatched would rewrite the
developer's ``~/.gitconfig``.  The auto-update test directories point
``GIT_CONFIG_GLOBAL`` at a fresh file per test through an autouse fixture
built on these helpers.  The file carries the developer's identity
(``user.name`` / ``user.email`` only), so tests that commit keep working.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest

IDENTITY_KEYS = ("user.name", "user.email")


def git_identity() -> List[Tuple[str, str]]:
    """The caller's global identity keys that are set (read-only)."""
    identity = []
    for key in IDENTITY_KEYS:
        result = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            identity.append((key, result.stdout.strip()))
    return identity


def point_global_git_config_at_scratch(
    directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: List[Tuple[str, str]],
) -> Path:
    """Write a fresh global config holding *identity* under *directory* and
    point ``GIT_CONFIG_GLOBAL`` at it (restored by *monkeypatch*)."""
    config = directory / "global-gitconfig"
    config.write_text("")
    for key, value in identity:
        subprocess.run(
            ["git", "config", "--file", str(config), key, value],
            check=True,
            capture_output=True,
        )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    return config
