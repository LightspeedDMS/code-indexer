"""HEAD's commit and branch, asked of local git or of the git seam.

An index records the commit and branch it was built from: in its metadata
(``current_commit``, which refresh compares with the repository's HEAD to
decide whether the index is stale) and on every chunk. Both used to come from
git run on THIS machine. On a pod that does not hold the repository there is
nothing to run it against, so both read "unknown" -- and refresh, seeing an
index that never matches HEAD, re-indexed from scratch every cycle.

With a git backend (git-mcp) the same two questions go through the seam. With
none, the commands are exactly the local ones they always were.

Failures degrade to ``"unknown"`` as before; they are not silent, because the
metadata then shows "unknown" and refresh forces a reconcile that is logged.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Optional, Tuple, Union

from ..server.storage.shared.git_backend import head_branch
from ..utils.git_runner import run_git_command

logger = logging.getLogger(__name__)

UNKNOWN = "unknown"

#: What git says for a path that is not inside a repository. The ONLY failure
#: that means "not a repository"; anything else (git-mcp unreachable, a timeout)
#: says nothing about the path and must not be read as an answer.
NOT_A_REPOSITORY_MARKER = "not a git repository"


def probe_backend_repository(
    project_dir: Union[str, Path], git_backend: Any
) -> Tuple[bool, Optional[str]]:
    """``(is_repository, head_commit)`` as the git backend answers them.

    Resolving HEAD answers both at once. ONLY git's explicit "not a git
    repository" makes the path non-git; a transient failure answers neither
    question, so it is reported as a repository with an unknown commit -- the
    safe side, since the other one switches a real repository's file
    signatures to file hashes and changes every point id.
    """
    try:
        commit = str(git_backend.rev_parse(str(project_dir), "HEAD")).strip()
    except subprocess.CalledProcessError as exc:
        return NOT_A_REPOSITORY_MARKER not in str(exc.stderr or ""), None
    except Exception as exc:  # the backend's transport, whatever it is
        logger.debug("git backend probe failed for %s: %s", project_dir, exc)
        return True, None
    return True, commit or None


def read_head_commit(project_dir: Union[str, Path], git_backend: Optional[Any]) -> str:
    """HEAD's full commit hash, or ``"unknown"``."""
    try:
        if git_backend is not None:
            commit = git_backend.rev_parse(str(project_dir), "HEAD")
        else:
            commit = run_git_command(
                ["git", "rev-parse", "HEAD"], cwd=Path(project_dir)
            ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug("HEAD commit unavailable for %s: %s", project_dir, exc)
        return UNKNOWN
    return str(commit).strip() or UNKNOWN


def read_head_branch(project_dir: Union[str, Path], git_backend: Optional[Any]) -> str:
    """The checked-out branch, ``"detached-<short sha>"``, or ``"unknown"``."""
    try:
        if git_backend is not None:
            branch: Optional[str] = head_branch(git_backend, str(project_dir))
        else:
            branch = (
                run_git_command(
                    ["git", "branch", "--show-current"], cwd=Path(project_dir)
                ).stdout.strip()
                or None
            )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug("HEAD branch unavailable for %s: %s", project_dir, exc)
        if git_backend is not None:
            # head_branch reports a real detached HEAD as None, so this is a
            # FAILURE. Reading it as detached (via the short hash) would record
            # a branch change that did not happen -- and detect_branch_change
            # would force a re-index. Unknown is the honest answer.
            return UNKNOWN
        # The local path keeps its long-standing fallback to the short hash.
        branch = None
    if branch:
        return branch
    try:
        if git_backend is not None:
            short = git_backend.rev_parse(str(project_dir), "--short", "HEAD")
        else:
            short = run_git_command(
                ["git", "rev-parse", "--short", "HEAD"], cwd=Path(project_dir)
            ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.debug("HEAD short hash unavailable for %s: %s", project_dir, exc)
        return UNKNOWN
    short = str(short).strip()
    return f"detached-{short}" if short else UNKNOWN
