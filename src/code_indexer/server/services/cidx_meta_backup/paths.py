"""Canonical path resolution for Story #926 cidx-meta backup.

Single source of truth for the mutable cidx-meta base directory.  Both
the Web UI save-config route and the RefreshScheduler backup path import
this helper so neither can drift from the other.
"""

from __future__ import annotations

from pathlib import Path


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
