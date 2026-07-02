"""Unit test for Story #926 AC4 — conflict-resolved path.

Uses a real git repo (tmp_path) with a real file:// bare remote to exercise
the full git rebase mechanics.  The resolver is an explicit seam (injectable
dependency) per the CidxMetaBackupSync constructor; FakeDiskMutatingResolver
is the concrete test double that stands in for Claude CLI.

Placed under tests/unit/ because test doubles are permitted at this layer.
"""

import subprocess
from pathlib import Path


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


class FakeDiskMutatingResolver:
    """Concrete resolver seam that writes resolved content and stages it.

    Simulates a successful Claude resolution without making any network calls.
    All git operations (add) are real subprocess calls.

    Validates that the branch argument passed by CidxMetaBackupSync matches
    the expected branch so callers can assert the full branch-propagation path.
    """

    def __init__(self, resolved_content: str, expected_branch: str) -> None:
        self.resolved_content = resolved_content
        self.expected_branch = expected_branch
        self.was_called = False
        self.called_with_files: list[str] = []
        self.received_branch: str = ""

    def resolve(self, cidx_meta_path: str, conflict_files: list[str], branch: str):
        from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
            ResolverResult,
        )

        self.was_called = True
        self.called_with_files = list(conflict_files)
        self.received_branch = branch
        assert branch == self.expected_branch, (
            f"Resolver received branch {branch!r} but expected {self.expected_branch!r}"
        )
        repo = Path(cidx_meta_path)
        for rel_path in conflict_files:
            target = repo / rel_path
            target.write_text(self.resolved_content, encoding="utf-8")
            subprocess.run(
                ["git", "add", rel_path],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )
        return ResolverResult(success=True, error=None)


def test_conflict_resolved_path_completes_rebase_and_pushes(tmp_path: Path) -> None:
    """# Story #926 AC4: sync completes and pushes when resolver clears all conflicts.

    Both sides modify the same line.  FakeDiskMutatingResolver writes resolved
    content to disk and stages it.  The rebase must continue and the resolved
    content must land in the remote.  The detected branch is passed end-to-end
    from init through push and sync.
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

    resolved = "resolved content\n"
    resolver = FakeDiskMutatingResolver(
        resolved_content=resolved, expected_branch=branch
    )

    result = CidxMetaBackupSync(str(repo), branch, resolver).sync()

    assert result.skipped is False
    assert result.sync_failure is None
    assert resolver.was_called is True
    assert shared_file in resolver.called_with_files
    assert resolver.received_branch == branch

    # Resolved content must be present in the remote
    verify = tmp_path / "verify"
    _clone(remote, verify)
    assert (verify / shared_file).read_text(encoding="utf-8") == resolved
