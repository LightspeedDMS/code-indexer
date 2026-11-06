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

#: What git says when HEAD names a branch that has no commit yet: a repository
#: created with nothing committed (``git init``, an empty remote). git's own,
#: definitive answer -- the repository exists and holds no commit -- so it is a
#: state ("no commit"), not a failure. ``rev-parse HEAD``, ``rev-parse
#: --abbrev-ref HEAD`` and ``rev-list --count HEAD`` all report it this way.
#: The closing quote keeps ``HEAD~1`` and other revisions out of it.
UNBORN_HEAD_MARKER = "ambiguous argument 'HEAD'"

#: Why a golden repository with no commit -- in its clone or upstream -- is
#: skipped, as refresh and the IndexStore backfill report it (EVO-64616).
EMPTY_REPOSITORY_REASON = (
    "empty repository: no commit in the clone or upstream; nothing to check "
    "out, pull or index until the first commit is pushed"
)


def is_unborn_head_stderr(stderr: object) -> bool:
    """True when git's *stderr* says HEAD has no commit yet (an empty repo)."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return UNBORN_HEAD_MARKER in str(stderr or "")


def is_unborn_head(exc: BaseException) -> bool:
    """True when *exc* is git's answer that HEAD has no commit yet.

    Only a ``CalledProcessError`` -- what both the local backend and git-mcp
    raise for a git failure -- can carry that answer; a transport failure says
    nothing about the repository and is never read as one.
    """
    return isinstance(exc, subprocess.CalledProcessError) and is_unborn_head_stderr(
        exc.stderr
    )


def head_is_unborn(git_backend: Any, repo_path: str, timeout: float = 10) -> bool:
    """True when the clone at *repo_path* has no commit yet (an empty clone),
    asked through the git seam with ``rev-parse HEAD``. Any other failure
    raises: only git's own unborn-HEAD answer means "empty"."""
    try:
        git_backend.rev_parse(repo_path, "HEAD", timeout=timeout)
    except subprocess.CalledProcessError as exc:
        if is_unborn_head(exc):
            return True
        raise
    return False


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
