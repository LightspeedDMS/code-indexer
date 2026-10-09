"""Refresh's git questions go through the git seam, so they work on a pod that
does not hold the repository.

On a mount-less pod the golden repo's directory holds only ``.code-indexer``.
Refresh used to ask git about it with raw subprocess calls run HERE; they
failed, and the failure read as an answer -- "detached HEAD, nothing to pull",
"no stale index" -- so refresh silently never pulled. Pinned against exactly
that shape (an empty directory) with a git backend that answers for the
repository elsewhere, as git-mcp does:

* a detached HEAD is the ONLY failure read as "no branch"; any other failure
  (git-mcp unreachable, not a repository) raises instead of skipping the pull;
* has_changes() reports what the backend says is upstream, never "up to date"
  by default;
* the stale-index check compares the index's recorded commit with the
  backend's HEAD.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, List, Optional

import pytest

from code_indexer.global_repos.git_pull_updater import GitPullUpdater
from code_indexer.server.storage.shared.git_backend import head_branch

DETACHED = "fatal: ref HEAD is not a symbolic ref"


class _GitMcp:
    """Answers for a repository that is not on this disk."""

    owns_credentials = True

    def __init__(
        self,
        *,
        branch: Optional[str] = "main",
        behind: int = 0,
        head: str = "a" * 40,
        symref_error: Optional[str] = None,
    ) -> None:
        self.branch = branch
        self.behind = behind
        self.head = head
        self.symref_error = symref_error
        self.calls: List[str] = []

    def fetch(self, repo_path: str, **kw: Any) -> None:
        self.calls.append("fetch")

    def symbolic_ref(self, repo_path: str, short: bool = True, **kw: Any) -> str:
        self.calls.append("symbolic_ref")
        if self.symref_error is not None:
            raise subprocess.CalledProcessError(1, "git-mcp", stderr=self.symref_error)
        if self.branch is None:
            raise subprocess.CalledProcessError(128, "git-mcp", stderr=DETACHED)
        return self.branch

    def rev_list_count(self, repo_path: str, range_expr: str, **kw: Any) -> int:
        self.calls.append(f"rev_list_count {range_expr}")
        return self.behind

    def rev_parse(self, repo_path: str, *args: str, **kw: Any) -> str:
        self.calls.append("rev_parse " + " ".join(args))
        return self.head


@pytest.fixture
def mountless_repo(tmp_path: Path) -> Path:
    """What a mount-less pod holds at the golden repo's path: index state only."""
    repo = tmp_path / "golden-repos" / "agent-router"
    (repo / ".code-indexer").mkdir(parents=True)
    return repo


# ── head_branch ─────────────────────────────────────────────────────────────


def test_head_branch_names_the_branch(mountless_repo: Path) -> None:
    assert head_branch(_GitMcp(branch="master"), str(mountless_repo)) == "master"


def test_only_a_detached_head_reads_as_no_branch(mountless_repo: Path) -> None:
    assert head_branch(_GitMcp(branch=None), str(mountless_repo)) is None


@pytest.mark.parametrize(
    "stderr",
    [
        "git-mcp git_symbolic_ref request to http://git-mcp:3000 failed: refused",
        "fatal: not a git repository (or any of the parent directories): .git",
    ],
)
def test_any_other_failure_raises_rather_than_reading_as_detached(
    mountless_repo: Path, stderr: str
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        head_branch(_GitMcp(symref_error=stderr), str(mountless_repo))


# ── has_changes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("behind, expected", [(3, True), (0, False)])
def test_has_changes_asks_the_backend_what_is_upstream(
    mountless_repo: Path, behind: int, expected: bool
) -> None:
    git = _GitMcp(behind=behind)
    updater = GitPullUpdater(str(mountless_repo), git_backend=git)

    assert updater.has_changes() is expected
    assert git.calls == ["fetch", "symbolic_ref", "rev_list_count HEAD..@{upstream}"]


def test_a_pinned_repository_still_skips_the_pull(mountless_repo: Path) -> None:
    git = _GitMcp(branch=None, behind=5)
    assert GitPullUpdater(str(mountless_repo), git_backend=git).has_changes() is False
    assert "rev_list_count HEAD..@{upstream}" not in git.calls


def test_an_unreachable_git_mcp_fails_the_check_instead_of_skipping(
    mountless_repo: Path,
) -> None:
    git = _GitMcp(symref_error="git-mcp git_symbolic_ref request failed: timed out")
    with pytest.raises(RuntimeError):
        GitPullUpdater(str(mountless_repo), git_backend=git).has_changes()


# ── stale-index check ───────────────────────────────────────────────────────


def _scheduler(git: _GitMcp) -> Any:
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = RefreshScheduler.__new__(RefreshScheduler)
    scheduler._git_backend = git
    return scheduler


def _record(repo: Path, commit: str) -> None:
    (repo / ".code-indexer" / "metadata-voyage-ai.json").write_text(
        json.dumps({"status": "completed", "current_commit": commit})
    )


def test_the_stale_index_check_compares_with_the_backends_head(
    mountless_repo: Path,
) -> None:
    git = _GitMcp(head="b" * 40)
    _record(mountless_repo, "a" * 40)

    assert _scheduler(git)._check_stale_index_metadata(
        str(mountless_repo), "agent-router-global"
    )
    assert "rev_parse HEAD" in git.calls


def test_an_index_at_the_backends_head_is_not_stale(mountless_repo: Path) -> None:
    _record(mountless_repo, "b" * 40)

    assert not _scheduler(_GitMcp(head="b" * 40))._check_stale_index_metadata(
        str(mountless_repo), "agent-router-global"
    )
