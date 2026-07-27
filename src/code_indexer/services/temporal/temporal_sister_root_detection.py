"""Structural golden-repo sister-root detection for standalone CLI/daemon/
watch temporal paths (GitHub Issue #1482 extension, site 4 -- LOW
priority/future-proofing).

Pure standalone CLI usage (an arbitrary user's own working repo -- the
overwhelmingly common case) has NO golden-owned sister location at all --
there is nothing to resolve, and this module must never invent one for
that case (this is the accepted Story #1460 R6 boundary: a standalone CLI
process has no cross-process coordination with the server's relocation
mechanism).

The ONE genuine case where a standalone cidx process (CLI query/status,
watch mode, the in-process daemon) IS looking at a golden repo's own
clone is an operator working directly inside the server's own
`~/.cidx-server/data/golden-repos/` tree, bypassing the server entirely --
e.g. `cd ~/.cidx-server/data/golden-repos/myrepo && cidx query ...
--time-range ...`. That tree has a fixed, well-established structural
layout this project already relies on elsewhere (diagnostics_service.py,
golden_repo_manager.py, VersionedSnapshotManager):
  - flat:      <golden_repos_dir>/<alias>/
  - versioned: <golden_repos_dir>/.versioned/<alias>/v_*/

detect_golden_repo_sister_root() recognizes ONLY these two exact,
pre-existing structural shapes -- never a heuristic guess -- and returns
None for anything else (a plain user repo, a repo named coincidentally
the same as a version dir, a lookalike path with the wrong middle segment
name, a versioned-alias directory missing its v_* leaf, etc).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional, Union

GOLDEN_REPOS_DIR_NAME = "golden-repos"
VERSIONED_DIR_NAME = ".versioned"
VERSION_DIR_PREFIX = "v_"


class GoldenRepoSisterRoot(NamedTuple):
    """Structurally-detected golden-repo sister-location coordinates.

    Attributes:
        golden_repos_dir: The golden-owned root housing `aliases/` and
            `.versioned/{namespace}/v_*/` (Story #1457's `sister_root`).
        repo_alias: The golden repo's BARE alias (no `-global` suffix).
    """

    golden_repos_dir: Path
    repo_alias: str


def detect_golden_repo_sister_root(
    codebase_dir: Optional[Union[Path, str]],
) -> Optional[GoldenRepoSisterRoot]:
    """Structurally detect whether codebase_dir IS a golden repo's own
    clone, recognizing ONLY the two established on-disk layouts.

    Args:
        codebase_dir: The standalone process's own codebase directory
            (CLI `config.codebase_dir`, daemon `project_root`, watch-mode
            `project_root`). May be None.

    Returns:
        GoldenRepoSisterRoot(golden_repos_dir, repo_alias) if codebase_dir
        structurally matches the flat or versioned golden-repo clone
        layout, else None. Never raises -- an unresolvable/nonexistent
        path (Path.resolve() does not require the path to exist) simply
        fails the structural match and returns None.
    """
    if codebase_dir is None:
        return None

    resolved = Path(codebase_dir).resolve()

    # Flat layout: <golden_repos_dir>/<alias>/
    parent = resolved.parent
    if parent.name == GOLDEN_REPOS_DIR_NAME:
        return GoldenRepoSisterRoot(golden_repos_dir=parent, repo_alias=resolved.name)

    # Versioned layout: <golden_repos_dir>/.versioned/<alias>/v_*/
    if resolved.name.startswith(VERSION_DIR_PREFIX):
        alias_dir = resolved.parent
        versioned_dir = alias_dir.parent
        if (
            versioned_dir.name == VERSIONED_DIR_NAME
            and versioned_dir.parent.name == GOLDEN_REPOS_DIR_NAME
        ):
            return GoldenRepoSisterRoot(
                golden_repos_dir=versioned_dir.parent, repo_alias=alias_dir.name
            )

    return None
