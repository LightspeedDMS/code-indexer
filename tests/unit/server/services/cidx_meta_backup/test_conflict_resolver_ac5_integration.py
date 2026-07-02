"""Unit test for Story #926 AC5 — conflict-resolution-failure path.

Uses a real git repo (tmp_path) with a real file:// bare remote.
FakeFailingResolver is a concrete injectable test double that returns failure.
Validates: RuntimeError is raised with the resolver error message, the working
tree is clean after git rebase --abort, and no push is attempted.

Post-abort state explanation:
  CidxMetaBackupSync.sync() commits local changes BEFORE starting the rebase.
  After rebase --abort, git restores the repo to that pre-rebase-started state:
  the local commit is intact, no conflict markers, working tree is clean.

Placed under tests/unit/ because test doubles are permitted at this layer.
"""

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def _configure_git_identity(repo: Path) -> None:
    """Set a deterministic git identity for test repos."""
    _git(["config", "user.email", "test@test.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)


def _clone(remote: Path, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


class FakeFailingResolver:
    """Concrete resolver seam that always returns failure.

    Simulates the case where Claude CLI cannot resolve the conflict
    (e.g., timeout or unrecognised conflict markers).
    """

    ERROR_MSG = "resolver mock error"

    def __init__(self) -> None:
        self.was_called = False
        self.called_with_files: list[str] = []

    def resolve(self, cidx_meta_path: str, conflict_files: list[str], branch: str):
        from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
            ResolverResult,
        )

        self.was_called = True
        self.called_with_files = list(conflict_files)
        return ResolverResult(success=False, error=self.ERROR_MSG)


def test_conflict_failed_path_aborts_rebase_and_raises(tmp_path: Path) -> None:
    """# Story #926 AC5: when resolver fails, rebase is aborted and RuntimeError is raised.

    Both sides modify the same file.  FakeFailingResolver returns failure.
    The sync must:
    - raise RuntimeError containing both "conflict resolution failed" and the
      resolver-specific error message
    - leave the working tree clean (git rebase --abort restores the committed
      local state, not the pre-commit dirty state)
    - NOT attempt a push (remote remains at the divergent commit)
    """
    from code_indexer.server.services.cidx_meta_backup.bootstrap import (
        CidxMetaBackupBootstrap,
    )
    from code_indexer.server.services.cidx_meta_backup.sync import CidxMetaBackupSync

    shared_file = "shared.txt"

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    # Write the seed file before bootstrap so it is included in the initial commit.
    # Do NOT call git init here — CidxMetaBackupBootstrap.bootstrap() handles
    # init + initial commit when .git/ does not exist.
    (repo / shared_file).write_text("original line\n", encoding="utf-8")
    CidxMetaBackupBootstrap().bootstrap(str(repo), remote.as_uri())
    # Read the actual branch from git after bootstrap so all subsequent
    # operations use the real detected value regardless of local git config.
    branch = _git(["branch", "--show-current"], repo)

    # Divergent clone modifies the shared file and pushes
    divergent = tmp_path / "divergent"
    _clone(remote, divergent)
    _configure_git_identity(divergent)
    (divergent / shared_file).write_text("remote change\n", encoding="utf-8")
    _git(["add", "-A"], divergent)
    _git(["commit", "-m", "remote: change shared file"], divergent)
    _git(["push", "origin", branch], divergent)

    # Local repo modifies the same line → conflict on rebase
    (repo / shared_file).write_text("local change\n", encoding="utf-8")

    # Use ls-remote against the bare remote URI so we read the real remote state,
    # not the local tracking ref (which is updated by `git fetch` inside sync()).
    remote_commit_before = _git(
        ["ls-remote", str(remote.as_uri()), f"refs/heads/{branch}"], repo
    ).split()[0]

    resolver = FakeFailingResolver()

    with pytest.raises(RuntimeError) as exc_info:
        CidxMetaBackupSync(str(repo), branch, resolver).sync()

    error_text = str(exc_info.value)
    assert "conflict resolution failed" in error_text
    assert FakeFailingResolver.ERROR_MSG in error_text

    assert resolver.was_called is True
    assert shared_file in resolver.called_with_files

    # Working tree must be clean: git rebase --abort restored the repo to the
    # committed local state (the sync always commits before rebasing).
    status = _git(["status", "--porcelain"], repo)
    assert status == "", (
        f"Expected clean working tree after rebase --abort, got: {status!r}"
    )

    # No conflict markers must remain in the file
    assert "<<<<<<" not in (repo / shared_file).read_text(encoding="utf-8")

    # No push must have been attempted: remote origin must still be at the
    # divergent commit, not at any local commit.
    remote_commit_after = _git(
        ["ls-remote", str(remote.as_uri()), f"refs/heads/{branch}"], repo
    ).split()[0]
    assert remote_commit_after == remote_commit_before
