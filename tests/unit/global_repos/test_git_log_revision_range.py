"""`GitOperationsService.get_log` (global_repos -- a SEPARATE
implementation from the server's own GitOperationsService) must accept a
two-dot/three-dot revision RANGE in `branch`, not just a single revision
expression, since `branch` reaches `git log`/`git rev-list --count`
unchanged: the only hazard is the whole value starting with '-' (read as
a git OPTION); beyond that, git itself resolves or rejects the value.

All tests run against a REAL throwaway git repository (no mocking of git
itself, per this repo's Anti-Mock rule).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.global_repos.git_operations import GitOperationsService
from code_indexer.server.services.git_argv_safety import GitArgumentValidationError


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(["init", "-q", "-b", "main"], repo_path)
    _git(["config", "user.email", "test@example.com"], repo_path)
    _git(["config", "user.name", "Test User"], repo_path)
    (repo_path / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo_path)
    _git(["commit", "-q", "-m", "c1 on main"], repo_path)
    _git(["checkout", "-q", "-b", "feature"], repo_path)
    (repo_path / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo_path)
    _git(["commit", "-q", "-m", "c2 on feature"], repo_path)
    _git(["checkout", "-q", "main"], repo_path)
    return repo_path


class TestGetLogBranchRevisionRange:
    def test_two_dot_range_returns_only_feature_only_commit(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch="main..feature")
        assert len(result.commits) == 1
        assert result.commits[0].subject == "c2 on feature"

    def test_accepts_option_shaped_range_side_deferred_to_git(
        self, repo: Path, tmp_path: Path
    ):
        """`main..--output=...` is not an option: the whole value starts
        with 'm'. There is no per-side check, so this reaches `git log`
        unchanged; `get_log`'s own pre-existing `except
        subprocess.CalledProcessError` swallows the resulting git failure
        and returns an empty result, the same as before any validation
        existed."""
        marker = tmp_path / "get_log_range_output_marker.txt"
        svc = GitOperationsService(repo)

        result = svc.get_log(branch=f"main..--output={marker}")
        assert result.commits == []
        assert not marker.exists()

    def test_accepts_unresolvable_side_deferred_to_git(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch="main..no-such-ref")
        assert result.commits == []

    def test_rejects_whole_value_starting_with_dash(self, repo: Path):
        svc = GitOperationsService(repo)
        with pytest.raises(GitArgumentValidationError):
            svc.get_log(branch="-x..feature")
