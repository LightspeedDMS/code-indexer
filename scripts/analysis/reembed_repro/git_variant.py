"""Git variant of the synthetic repository (design 15.1 additions, story S0).

The repository is committed on ``main``; before each refresh the sync job
commits its new files, and one seeded operation follows:

* ``edit``   -- append a unique line to 3 tracked files, left uncommitted
  (content is always new, never a revert to content seen before);
* ``rename`` -- ``git mv`` 3 files into ``renamed/`` and commit (same content);
* ``branch`` -- commit pending edits, then switch ``main`` -> ``feature-N``
  (whose commit adds ``features/feature-N.md``) or back to ``main``.

Only the throwaway scratch repository is touched; commits use a neutral
example identity.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import List, Optional

from synthetic_repo import list_repo_files

GIT_OPS = ("edit", "rename", "branch")
_FILES_PER_OP = 3
_IDENTITY = [
    "-c",
    "user.name=reembed-repro",
    "-c",
    "user.email=reembed-repro@example.com",
    "-c",
    "commit.gpgsign=false",
]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_IDENTITY, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def init_git_repo(repo: Path) -> None:
    (repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")


def commit_sync(repo: Path, paths: List[str], tag: str) -> None:
    _git(repo, "add", "--", *paths)
    _git(repo, "commit", "-q", "-m", f"sync {tag}")


def current_branch(repo: Path) -> Optional[str]:
    """Branch key of the working tree: None outside git, 'HEAD' when detached."""
    if not (repo / ".git").exists():
        return None
    return _git(repo, "branch", "--show-current").strip() or "HEAD"


def choose_op(rng: random.Random) -> str:
    return rng.choice(GIT_OPS)


def _commit_pending(repo: Path, message: str) -> None:
    if _git(repo, "status", "--porcelain").strip():
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", message)


def apply_op(repo: Path, op: str, index: int, rng: random.Random) -> str:
    """Apply one git operation; return a one-line description."""
    files = rng.sample(list_repo_files(repo), _FILES_PER_OP)
    if op == "edit":
        for k, rel in enumerate(files):
            with open(repo / rel, "a", encoding="utf-8") as handle:
                handle.write(f"\nedit {index}-{k}: uncommitted change\n")
        return f"edit {len(files)} files (uncommitted)"
    if op == "rename":
        for k, rel in enumerate(files):
            dest = f"renamed/{index}-{k}-{Path(rel).name}"
            (repo / dest).parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "mv", rel, dest)
        _git(repo, "commit", "-q", "-m", f"rename {index}")
        return f"rename {len(files)} files (committed)"
    if op == "branch":
        _commit_pending(repo, f"pending edits before switch {index}")
        if current_branch(repo) == "main":
            _git(repo, "checkout", "-q", "-b", f"feature-{index}")
            note = repo / "features" / f"feature-{index}.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(f"# Feature {index}\n\nbranch-only content {index}\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", f"feature {index}")
            return f"switch main -> feature-{index}"
        _git(repo, "checkout", "-q", "main")
        return "switch -> main"
    raise ValueError(f"unknown git op {op!r}")
