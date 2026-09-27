"""Argument-safety tests for `GitOperationsService` (global_repos) --
a SEPARATE implementation from the server's own GitOperationsService,
backing the read-only git MCP tools (`git_log`'s omni/multi-repo path,
`git_show_commit`, `git_file_at_revision`, `git_blame`).

`get_log`'s `branch`, `show_commit`'s `commit_hash`, `get_file_at_revision`'s
`revision`, and `get_blame`'s `revision` are all free-form revision
expressions that reach a real `git` subprocess argv. Each must reject a
leading '-' before any subprocess runs, via the same shared
`validate_revision` used by the server's GitOperationsService.

All tests run against a REAL throwaway git repository (no mocking of git
itself, per this repo's Anti-Mock rule).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.global_repos.git_operations import BlameResult, GitOperationsService
from code_indexer.server.services.git_argv_safety import GitArgumentValidationError


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(["init", "-q"], repo_path)
    _git(["config", "user.email", "test@example.com"], repo_path)
    _git(["config", "user.name", "Test User"], repo_path)
    _git(["checkout", "-q", "-b", "main"], repo_path)
    (repo_path / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo_path)
    _git(["commit", "-q", "-m", "first"], repo_path)
    (repo_path / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo_path)
    _git(["commit", "-q", "-m", "second"], repo_path)
    _git(["tag", "v1"], repo_path)
    _git(["checkout", "-q", "-b", "feature/x"], repo_path)
    _git(["checkout", "-q", "main"], repo_path)
    return repo_path


class TestGetLogBranchValidation:
    def test_rejects_output_option_as_branch(self, repo: Path, tmp_path: Path):
        marker = tmp_path / "get_log_output_marker.txt"
        svc = GitOperationsService(repo)

        with pytest.raises(GitArgumentValidationError):
            svc.get_log(branch=f"--output={marker}")

        assert not marker.exists()

    def test_accepts_head_tilde_1(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch="HEAD~1")
        assert result.total_count == 1

    def test_accepts_branch_with_slash(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch="feature/x")
        assert result.total_count == 2

    def test_accepts_tag(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch="v1")
        assert result.total_count == 2

    def test_accepts_none_defaults_to_head(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_log(branch=None)
        assert result.total_count == 2


class TestShowCommitHashValidation:
    def test_rejects_option_shaped_commit_hash(self, repo: Path, tmp_path: Path):
        marker = tmp_path / "show_commit_marker.txt"
        svc = GitOperationsService(repo)

        with pytest.raises(GitArgumentValidationError):
            svc.show_commit(commit_hash=f"--output={marker}")

        assert not marker.exists()

    def test_accepts_head(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.show_commit(commit_hash="HEAD")
        assert result.commit.subject == "second"

    def test_accepts_full_sha(self, repo: Path):
        sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        svc = GitOperationsService(repo)
        result = svc.show_commit(commit_hash=sha)
        assert result.commit.hash == sha


class TestGetFileAtRevisionValidation:
    def test_rejects_option_shaped_revision(self, repo: Path):
        svc = GitOperationsService(repo)

        with pytest.raises(GitArgumentValidationError):
            svc.get_file_at_revision(path="f.txt", revision="-x")

    def test_accepts_head_tilde_1(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_file_at_revision(path="f.txt", revision="HEAD~1")
        assert result.content == "one\n"

    def test_accepts_tag(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_file_at_revision(path="f.txt", revision="v1")
        assert result.content == "one\ntwo\n"


class TestGetBlameRevisionValidation:
    def test_rejects_option_shaped_revision(self, repo: Path, tmp_path: Path):
        outside_file = tmp_path / "outside_marker.txt"
        outside_file.write_text("marker-content\n")
        svc = GitOperationsService(repo)

        with pytest.raises(GitArgumentValidationError):
            svc.get_blame(path="f.txt", revision=f"--contents={outside_file}")

    def test_accepts_head(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_blame(path="f.txt", revision="HEAD")
        assert isinstance(result, BlameResult)
        assert len(result.lines) == 2

    def test_accepts_branch_with_slash(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_blame(path="f.txt", revision="feature/x")
        assert isinstance(result, BlameResult)
        assert len(result.lines) == 2

    def test_accepts_none_defaults_to_head(self, repo: Path):
        svc = GitOperationsService(repo)
        result = svc.get_blame(path="f.txt", revision=None)
        assert isinstance(result, BlameResult)
        assert result.revision == "HEAD"


class TestGetDiffRevisionValidation:
    """`get_diff` builds three `git diff` argvs with from_revision and
    to_revision as bare positionals, so each must not start with '-' or
    contain a control character; any other revision or range reaches git
    unchanged, and `path` still follows `--`."""

    @pytest.mark.parametrize("which", ["from_revision", "to_revision"])
    def test_rejects_output_option(self, repo: Path, tmp_path: Path, which: str):
        marker = tmp_path / "get_diff_output_marker.txt"
        option = f"--output={marker}"
        from_revision = option if which == "from_revision" else "HEAD~1"
        to_revision = option if which == "to_revision" else "HEAD"

        with pytest.raises(GitArgumentValidationError):
            GitOperationsService(repo).get_diff(
                from_revision=from_revision, to_revision=to_revision
            )

        assert not marker.exists()

    def test_rejects_control_character(self, repo: Path):
        with pytest.raises(GitArgumentValidationError):
            GitOperationsService(repo).get_diff(from_revision="HEAD~1\n")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"from_revision": "HEAD~1"},
            {"from_revision": "HEAD~1..HEAD"},
            {"from_revision": "HEAD~1", "to_revision": "HEAD", "path": "f.txt"},
        ],
    )
    def test_accepts_revisions_and_ranges(self, repo: Path, kwargs: dict):
        result = GitOperationsService(repo).get_diff(**kwargs)

        assert [f.path for f in result.files] == ["f.txt"]
        assert result.total_insertions == 1
