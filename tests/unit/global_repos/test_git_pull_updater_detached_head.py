"""
Tests for GitPullUpdater.has_changes() robustness when HEAD is detached
(golden repo pinned to a TAG or specific commit SHA).

Real git repos are used — no mocking of git subprocess calls.

Bug context: when a golden repo's `default_branch` is a TAG (e.g. 'v4.8.3'),
the clone is in DETACHED HEAD state.  The prior implementation ran:
    git log HEAD..@{upstream} --oneline
which fails with `fatal: HEAD does not point to a branch` (rc != 0), causing
has_changes() to raise RuntimeError on every refresh cycle.

Fix: detect detached HEAD via `git symbolic-ref -q HEAD` (rc != 0 when
detached) and return False immediately — a pinned/detached ref is immutable
for refresh purposes, there is nothing to pull.

Test matrix:
  1. Detached HEAD pinned to a TAG       -> has_changes() returns False, no raise
  2. Detached HEAD pinned to a COMMIT SHA -> returns False, no raise
  3. REGRESSION: branch, remote ahead    -> returns True
  4. REGRESSION: branch, up-to-date      -> returns False
  5. REGRESSION: branch, remote tag added but branch unchanged -> returns False
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.global_repos.git_pull_updater import GitPullUpdater


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run a git command in *cwd*, capturing output.  Raises on failure when check=True."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _make_remote_commit(remote: Path, tmp_parent: Path, filename: str) -> None:
    """
    Push one commit to *remote* via a fresh ephemeral clone.

    This avoids duplicating the three-step clone/config/commit/push pattern
    across multiple tests.
    """
    ephemeral = tmp_parent / f"_ephemeral_{filename}"
    ephemeral.mkdir()
    _git(["clone", str(remote), str(ephemeral)], cwd=tmp_parent)
    _git(["config", "user.email", "test2@cidx.test"], cwd=ephemeral)
    _git(["config", "user.name", "CIDX Test2"], cwd=ephemeral)
    (ephemeral / filename).write_text(f"content of {filename}")
    _git(["add", filename], cwd=ephemeral)
    _git(["commit", "-m", f"add {filename}"], cwd=ephemeral)
    _git(["push", "origin", "main"], cwd=ephemeral)


# ---------------------------------------------------------------------------
# Fixture: bare remote + working clone with initial commit
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> dict:  # type: ignore[type-arg]
    """
    Build a minimal real git environment:
        remote/   -- bare repository acting as the remote
        clone/    -- working clone (origin -> remote/)

    Returns a dict:
        {
            "remote": Path,  # bare remote dir
            "clone":  Path,  # working clone dir
            "initial_sha": str,  # SHA of initial commit
        }
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init", "--bare", "--initial-branch=main", str(remote)], cwd=tmp_path)

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(["clone", str(remote), str(clone)], cwd=tmp_path)

    _git(["config", "user.email", "test@cidx.test"], cwd=clone)
    _git(["config", "user.name", "CIDX Test"], cwd=clone)

    (clone / "README.md").write_text("initial")
    _git(["add", "README.md"], cwd=clone)
    _git(["commit", "-m", "initial commit"], cwd=clone)
    _git(["push", "origin", "main"], cwd=clone)

    initial_sha = _git(["rev-parse", "HEAD"], cwd=clone).stdout.strip()

    return {"remote": remote, "clone": clone, "initial_sha": initial_sha}


# ---------------------------------------------------------------------------
# 1. Detached HEAD pinned to a TAG -> returns False, does NOT raise
# ---------------------------------------------------------------------------


def test_detached_head_tag_pinned_returns_false(git_repo: dict) -> None:  # type: ignore[type-arg]
    """
    has_changes() returns False (and does NOT raise) for a clone pinned to a tag.
    The @{upstream} comparison is skipped because HEAD is detached.
    """
    clone: Path = git_repo["clone"]

    _git(["tag", "v1.0.0"], cwd=clone)
    _git(["push", "origin", "v1.0.0"], cwd=clone)
    _git(["checkout", "v1.0.0"], cwd=clone)

    # Verify we are truly in detached HEAD state
    sym = _git(["symbolic-ref", "-q", "HEAD"], cwd=clone, check=False)
    assert sym.returncode != 0, "Expected detached HEAD after tag checkout"

    updater = GitPullUpdater(str(clone))
    result = updater.has_changes()
    assert result is False


# ---------------------------------------------------------------------------
# 2. Detached HEAD pinned to a specific COMMIT SHA -> returns False, no raise
# ---------------------------------------------------------------------------


def test_detached_head_commit_sha_pinned_returns_false(git_repo: dict) -> None:  # type: ignore[type-arg]
    """
    has_changes() returns False for a clone pinned to a specific commit SHA.
    """
    clone: Path = git_repo["clone"]
    initial_sha: str = git_repo["initial_sha"]

    # Add a second commit so the initial SHA is no longer the branch tip
    (clone / "second.txt").write_text("second")
    _git(["add", "second.txt"], cwd=clone)
    _git(["commit", "-m", "second commit"], cwd=clone)
    _git(["push", "origin", "main"], cwd=clone)

    # Detach HEAD at the initial SHA
    _git(["checkout", initial_sha], cwd=clone)

    sym = _git(["symbolic-ref", "-q", "HEAD"], cwd=clone, check=False)
    assert sym.returncode != 0, "Expected detached HEAD after commit SHA checkout"

    updater = GitPullUpdater(str(clone))
    result = updater.has_changes()
    assert result is False


# ---------------------------------------------------------------------------
# 3. REGRESSION: branch with remote ahead -> returns True
# ---------------------------------------------------------------------------


def test_regression_branch_remote_ahead_returns_true(git_repo: dict) -> None:  # type: ignore[type-arg]
    """
    REGRESSION: when on a branch and the remote has new commits, has_changes() returns True.
    """
    clone: Path = git_repo["clone"]
    remote: Path = git_repo["remote"]

    _make_remote_commit(remote, remote.parent, "remote_change.txt")

    updater = GitPullUpdater(str(clone))
    result = updater.has_changes()
    assert result is True


# ---------------------------------------------------------------------------
# 4. REGRESSION: branch up-to-date -> returns False
# ---------------------------------------------------------------------------


def test_regression_branch_up_to_date_returns_false(git_repo: dict) -> None:  # type: ignore[type-arg]
    """
    REGRESSION: when on a branch and local is in sync with remote, has_changes() returns False.
    """
    clone: Path = git_repo["clone"]

    updater = GitPullUpdater(str(clone))
    result = updater.has_changes()
    assert result is False


# ---------------------------------------------------------------------------
# 5. REGRESSION: remote tag added, branch unchanged -> returns False
# ---------------------------------------------------------------------------


def test_regression_remote_tag_added_branch_unchanged_returns_false(
    git_repo: dict,
) -> None:  # type: ignore[type-arg]
    """
    REGRESSION: when the remote receives only a new tag (no branch commits),
    has_changes() returns False because HEAD..@{upstream} is empty.
    Tags do not constitute branch changes.
    """
    clone: Path = git_repo["clone"]

    # Push a tag from the clone — no new commits on main
    _git(["tag", "v0.9.0"], cwd=clone)
    _git(["push", "origin", "v0.9.0"], cwd=clone)

    updater = GitPullUpdater(str(clone))
    result = updater.has_changes()
    assert result is False
