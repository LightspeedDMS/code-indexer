"""Canonical path resolution for Story #926 cidx-meta backup.

Single source of truth for the mutable cidx-meta base directory.  Both
the Web UI save-config route and the RefreshScheduler backup path import
this helper so neither can drift from the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union


def cidx_meta_is_git_repository(cidx_meta_path: Union[str, Path]) -> bool:
    """True when cidx-meta is a git repository (``.git`` present, directory
    or file). It becomes one only when the cidx-meta backup bootstrap runs
    ``git init``; until then writers keep plain files and make no git calls.
    """
    return (Path(cidx_meta_path) / ".git").exists()


def get_cidx_meta_path(server_data_dir: Path) -> Path:
    """Return the mutable base path for cidx-meta.

    This is the directory where git operations and indexing happen.
    It is NEVER a .versioned/ snapshot path.

    The canonical layout is:
        <server_data_dir>/data/golden-repos/cidx-meta/

    This mirrors the golden_repos_dir construction used in lifespan.py:
        golden_repos_dir = Path(server_data_dir) / "data" / "golden-repos"

    Args:
        server_data_dir: Root server data directory (e.g. ~/.cidx-server).

    Returns:
        Path to the mutable cidx-meta directory (may not exist yet).
    """
    return Path(server_data_dir) / "data" / "golden-repos" / "cidx-meta"
