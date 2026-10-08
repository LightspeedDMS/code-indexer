"""Values that are never a git OPTION reach git unchanged, so git itself
resolves or rejects them exactly as it would with no validation at all.

- The exact value `-` is a single character, not `-<something>`: at every
  branch/refspec site it is a positional (`git checkout -`, `git branch -`,
  `git branch -d -`, `git push <remote> -`, `git pull <remote> -`, and the
  PAT push's `HEAD:refs/heads/-`), so the outcome is git's own.
- An empty reset mode is not an option: the call site's `f"--{mode}"`
  becomes a bare `--` separator, so the argv is `git reset -- <target>`
  and git reads the target (e.g. `HEAD`) as a PATHSPEC, not a commit --
  the same argv, and so the same git outcome, as with no validation.
- A pathspec always follows `--` in `git add`, `git reset HEAD` and the
  legacy `git diff` file_paths branch, so a filename that is literally
  `-`, starts with `-`, or contains LF is a literal path; only a NUL byte
  (never a legal path character) is rejected.

All tests run against REAL throwaway git repositories (no mocking of git
itself, per this repo's Anti-Mock rule).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_argv_safety import GitArgumentValidationError
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    git_operations_service,
)
from tests.unit.server.services._git_confirm_helpers import (
    singleton_confirmation_store_fixture,  # noqa: F401 -- registers the fixture
)

_LF_NAME = "line\nbreak.txt"


def _git(args: list, cwd: Path) -> str:
    out: str = subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout
    return out


def _rev(repo: Path, ref: str) -> str:
    return _git(["rev-parse", ref], repo).strip()


def _staged(repo: Path) -> list:
    return _git(["diff", "--cached", "--name-only", "-z"], repo).split("\0")[:-1]


@pytest.fixture()
def remote_and_repo(tmp_path: Path):
    remote = tmp_path / "golden.git"
    _git(["init", "-q", "--bare", str(remote)], tmp_path)
    repo = tmp_path / "repo"
    _git(["init", "-q", "-b", "main", str(repo)], tmp_path)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    _git(["remote", "add", "golden", str(remote)], repo)
    _git(["push", "-q", "-u", "golden", "main"], repo)
    _git(["checkout", "-q", "-b", "feature"], repo)
    _git(["checkout", "-q", "main"], repo)
    return remote, repo


@pytest.fixture()
def repo(remote_and_repo) -> Path:
    return Path(remote_and_repo[1])


class TestBareDashReachesGitAtBranchSites:
    def test_branch_switch_dash_checks_out_previous_branch(self, repo: Path):
        result = git_operations_service.git_branch_switch(repo, branch_name="-")

        assert result["success"] is True
        assert _git(["branch", "--show-current"], repo).strip() == "feature"

    def test_branch_create_dash_is_rejected_by_git_itself(self, repo: Path):
        with pytest.raises(GitCommandError) as exc_info:
            git_operations_service.git_branch_create(repo, branch_name="-")

        assert "not a valid branch name" in exc_info.value.stderr

    def test_branch_delete_dash_is_resolved_by_git_itself(
        self, repo: Path, singleton_confirmation_store
    ):
        binding = {"username": "alice", "repo_alias": "example-repo"}
        first = git_operations_service.git_branch_delete(
            repo, branch_name="-", **binding
        )
        assert first["requires_confirmation"] is True

        with pytest.raises(GitCommandError) as exc_info:
            git_operations_service.git_branch_delete(
                repo, branch_name="-", confirmation_token=first["token"], **binding
            )

        assert "branch '-' not found" in exc_info.value.stderr
        assert "feature" in _git(["branch"], repo)

    def test_push_dash_is_rejected_by_git_itself(self, repo: Path):
        with pytest.raises(GitCommandError) as exc_info:
            git_operations_service.git_push(repo, remote="golden", branch="-")

        assert "src refspec - does not match any" in exc_info.value.stderr

    def test_pull_dash_is_rejected_by_git_itself(self, repo: Path):
        result = git_operations_service.git_pull(repo, remote="golden", branch="-")

        assert result["success"] is False

    def test_push_with_pat_dash_pushes_literal_ref(self, remote_and_repo):
        remote, repo = remote_and_repo
        result = git_operations_service.git_push_with_pat(
            repo,
            remote="golden",
            branch="-",
            credential={"token": "unused-local-remote-token"},
        )

        assert result["success"] is True
        assert _rev(remote, "refs/heads/-") == _rev(repo, "HEAD")


class TestRefspecContentsReachGit:
    """A push/pull refspec is ONE argv element: only a leading '-' on the
    whole element makes it an option. A side of `src:dst`, or the part
    after a leading '+', is a ref name to git, never an option."""

    @pytest.mark.parametrize(
        "refspec,remote_ref",
        [("main:-", "refs/heads/-"), ("+main:-x", "refs/heads/-x")],
    )
    def test_push_creates_dash_named_remote_ref(
        self, remote_and_repo, refspec: str, remote_ref: str
    ):
        remote, repo = remote_and_repo

        result = git_operations_service.git_push(repo, remote="golden", branch=refspec)

        assert result["success"] is True
        assert _rev(remote, remote_ref) == _rev(repo, "main")

    def test_push_delete_of_dash_named_ref_is_resolved_by_git(self, repo: Path):
        with pytest.raises(GitCommandError) as exc_info:
            git_operations_service.git_push(repo, remote="golden", branch=":--x")

        assert "unable to delete '--x'" in exc_info.value.stderr

    def test_pull_dash_named_destination_reaches_git(self, repo: Path):
        result = git_operations_service.git_pull(repo, remote="golden", branch="main:-")

        assert "success" in result
        assert _rev(repo, "refs/heads/-") == _rev(repo, "main")


class TestRemoteIsAnyConfiguredName:
    """`remote` must be one of the repository's configured remotes (an
    arbitrary URL or path would let a caller push anywhere), but any
    configured name git itself accepts is usable: only a leading '-'
    (other than exactly `-`) and a control character are rejected."""

    def _add(self, remote_and_repo, name: str) -> Path:
        remote, repo = remote_and_repo
        _git(["remote", "add", "--", name, str(remote)], repo)
        return Path(repo)

    def test_push_pull_fetch_via_plus_named_remote(self, remote_and_repo):
        remote, _ = remote_and_repo
        repo = self._add(remote_and_repo, "a+b")

        pushed = git_operations_service.git_push(
            repo, remote="a+b", branch="main:refs/heads/via-plus"
        )
        pulled = git_operations_service.git_pull(repo, remote="a+b", branch="main")
        fetched = git_operations_service.git_fetch(repo, remote="a+b")

        assert pushed["success"] is True
        assert _rev(remote, "refs/heads/via-plus") == _rev(repo, "main")
        assert pulled["success"] is True
        assert fetched["success"] is True

    def test_push_via_remote_named_exactly_dash(self, remote_and_repo):
        remote, _ = remote_and_repo
        repo = self._add(remote_and_repo, "-")

        result = git_operations_service.git_push(
            repo, remote="-", branch="main:refs/heads/via-dash"
        )

        assert result["success"] is True
        assert _rev(remote, "refs/heads/via-dash") == _rev(repo, "main")

    def test_configured_dash_prefixed_remote_still_rejected(self, remote_and_repo):
        repo = self._add(remote_and_repo, "-x")

        with pytest.raises(GitArgumentValidationError, match="must not start"):
            git_operations_service.git_push(repo, remote="-x", branch="main")

    @pytest.mark.parametrize("which", ["path", "url"])
    def test_unconfigured_url_or_path_rejected(self, remote_and_repo, which: str):
        remote, repo = remote_and_repo
        target = str(remote) if which == "path" else "https://example.com/r.git"

        with pytest.raises(GitArgumentValidationError, match="not a configured remote"):
            git_operations_service.git_push(repo, remote=target, branch="main")

    @pytest.mark.parametrize("value", ["golden\n", "gol\rden", "a+b\n"])
    def test_control_character_rejected(self, remote_and_repo, value: str):
        repo = self._add(remote_and_repo, "a+b")

        with pytest.raises(GitArgumentValidationError, match="control character"):
            git_operations_service.git_push(repo, remote=value, branch="main")


class TestResetEmptyModeKeepsPathspecArgv:
    def test_empty_mode_resets_target_as_pathspec(self, repo: Path):
        head_before = _rev(repo, "HEAD")
        (repo / "f.txt").write_text("one\ntwo\n")
        _git(["add", "f.txt"], repo)

        result = git_operations_service.git_reset(repo, mode="")

        assert result == {"success": True, "reset_mode": "", "target_commit": "HEAD"}
        assert _rev(repo, "HEAD") == head_before
        # `git reset -- HEAD` treats HEAD as a (non-matching) pathspec, so
        # the staged change is untouched -- git's own outcome for that argv.
        assert _staged(repo) == ["f.txt"]

    def test_non_empty_abbreviation_still_rejected(self, repo: Path):
        with pytest.raises(GitArgumentValidationError):
            git_operations_service.git_reset(repo, mode="ha")


class TestPathspecsAreLiteralPaths:
    @pytest.mark.parametrize("name", ["-", _LF_NAME])
    def test_stage_real_file(self, repo: Path, name: str):
        (repo / name).write_text("content\n")

        result = git_operations_service.git_stage(repo, [name])

        assert result == {"success": True, "staged_files": [name]}
        assert _staged(repo) == [name]

    @pytest.mark.parametrize("name", ["-", _LF_NAME])
    def test_unstage_real_file(self, repo: Path, name: str):
        (repo / name).write_text("content\n")
        _git(["add", "--", name], repo)

        result = git_operations_service.git_unstage(repo, [name])

        assert result == {"success": True, "unstaged_files": [name]}
        assert _staged(repo) == []

    def test_stage_rejects_nul_before_git_runs(self, repo: Path):
        (repo / "a.txt").write_text("content\n")

        with pytest.raises(GitArgumentValidationError):
            git_operations_service.git_stage(repo, ["a.txt", "a\x00b"])

        assert _staged(repo) == []

    def test_unstage_rejects_nul_before_git_runs(self, repo: Path):
        (repo / "a.txt").write_text("content\n")
        _git(["add", "a.txt"], repo)

        with pytest.raises(GitArgumentValidationError):
            git_operations_service.git_unstage(repo, ["a.txt", "a\x00b"])

        assert _staged(repo) == ["a.txt"]
