"""Argument-safety tests for the remaining `GitOperationsService` git argv
sites: `git_log`'s `branch`, `git_stage`/`git_unstage`'s `file_paths`,
`git_reset`'s `commit_hash`, `merge_branch`'s `source_branch`, and
`git_branch_create`/`git_branch_switch`/`git_branch_delete`'s `branch_name`.

Every one of these values reaches a real `git` subprocess argv. Git parses
any positional starting with `-` as an OPTION, not a literal value, so
revisions and branch names are rejected before any subprocess runs when
they start with '-' (other than the exact value `-`, which is never an
option). Pathspecs always follow a `--` separator instead, so an
option-shaped path is a literal path that git itself resolves or rejects.

All tests run against a REAL throwaway git repository (no mocking of git
itself, per this repo's Anti-Mock rule). Rejection tests assert BOTH that
`GitArgumentValidationError` is raised AND that the corresponding side
effect (a marker file, a moved HEAD, an executable bit, a discarded local
edit) never happened -- proving the command never reached argv, not just
that an error surfaced afterward. Legitimate-usage tests prove the fix
does not break HEAD, HEAD~N, full/short SHAs, tags, or branch names with
slashes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_argv_safety import GitArgumentValidationError
from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
    git_operations_service,
)


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _rev_parse(repo_path: Path, ref: str) -> str:
    stdout: str = _git(["rev-parse", ref], repo_path).stdout
    return stdout.strip()


def _make_service() -> GitOperationsService:
    """The module's real singleton -- lazy-init config/timeouts resolve
    cleanly to defaults in this dev environment, so no
    ActivatedRepoManager/config bootstrapping is exercised by these
    tests."""
    return git_operations_service


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Real repo on branch 'main' with two commits, a tag, and a sibling
    'feature/nested' branch with one extra commit -- enough history to
    exercise HEAD, HEAD~1, a tag, a full SHA, and a slashed branch name."""
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
    _git(["checkout", "-q", "-b", "feature/nested"], repo_path)
    (repo_path / "f.txt").write_text("one\ntwo\nthree\n")
    _git(["add", "f.txt"], repo_path)
    _git(["commit", "-q", "-m", "feature commit"], repo_path)
    _git(["checkout", "-q", "main"], repo_path)
    return repo_path


# ---------------------------------------------------------------------------
# git_log branch argument validation
# ---------------------------------------------------------------------------


class TestGitLogBranchValidation:
    def test_rejects_output_option_as_branch(self, repo: Path, tmp_path: Path):
        marker = tmp_path / "git_log_output_marker.txt"
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_log(repo, branch=f"--output={marker}")

        assert not marker.exists(), (
            "git_log must never execute an option value passed as `branch`"
        )

    def test_accepts_head_tilde_1(self, repo: Path):
        svc = _make_service()
        result = svc.git_log(repo, branch="HEAD~1")
        assert result["total_commits"] == 1

    def test_accepts_branch_name_with_slash(self, repo: Path):
        svc = _make_service()
        result = svc.git_log(repo, branch="feature/nested")
        assert result["total_commits"] == 3

    def test_accepts_tag(self, repo: Path):
        svc = _make_service()
        result = svc.git_log(repo, branch="v1")
        assert result["total_commits"] == 2

    def test_accepts_full_sha(self, repo: Path):
        sha = _rev_parse(repo, "HEAD")
        svc = _make_service()
        result = svc.git_log(repo, branch=sha)
        assert result["total_commits"] == 2

    def test_accepts_none_defaults_to_head(self, repo: Path):
        svc = _make_service()
        result = svc.git_log(repo, branch=None)
        assert result["total_commits"] == 2


# ---------------------------------------------------------------------------
# git_stage / git_unstage pathspec argument validation
# ---------------------------------------------------------------------------


class TestGitStagePathspecValidation:
    def test_option_shaped_pathspec_follows_separator(self, repo: Path):
        """`git add -- <paths>`: an option-shaped entry is a literal
        pathspec, so git itself rejects the non-matching `--chmod=+x`
        path and stages nothing. Without the separator, git would read it
        as an option and stage exec_me.txt with mode 100755."""
        (repo / "exec_me.txt").write_text("plain file\n")
        svc = _make_service()

        with pytest.raises(GitCommandError) as exc_info:
            svc.git_stage(repo, ["--chmod=+x", "exec_me.txt"])

        assert "pathspec '--chmod=+x' did not match" in exc_info.value.stderr
        status = _git(["status", "--porcelain"], repo).stdout
        # Still untracked ("??"), never staged ("A ")
        assert "?? exec_me.txt" in status
        assert _git(["ls-files", "-s", "exec_me.txt"], repo).stdout == ""

    def test_accepts_path_with_spaces(self, repo: Path):
        (repo / "my file.txt").write_text("content\n")
        svc = _make_service()

        result = svc.git_stage(repo, ["my file.txt"])

        assert result["success"] is True
        status = _git(["status", "--porcelain"], repo).stdout
        # git quotes a pathspec containing a space in --porcelain output.
        assert 'A  "my file.txt"' in status


class TestGitUnstagePathspecValidation:
    def test_option_shaped_pathspec_follows_separator(self, repo: Path):
        """`git reset HEAD -- <paths>`: a file literally named `--hard` is
        unstaged as a path, and an unrelated dirty edit survives. Without
        the separator, `git reset HEAD --hard` would discard that edit."""
        (repo / "--hard").write_text("content\n")
        _git(["add", "--", "--hard"], repo)
        (repo / "f.txt").write_text("uncommitted local edit\n")
        svc = _make_service()

        result = svc.git_unstage(repo, ["--hard"])

        assert result == {"success": True, "unstaged_files": ["--hard"]}
        assert _git(["diff", "--cached", "--name-only"], repo).stdout == ""
        assert (repo / "f.txt").read_text() == "uncommitted local edit\n"
        assert "?? --hard" in _git(["status", "--porcelain"], repo).stdout

    def test_accepts_normal_path(self, repo: Path):
        (repo / "staged2.txt").write_text("content\n")
        _git(["add", "staged2.txt"], repo)
        svc = _make_service()

        result = svc.git_unstage(repo, ["staged2.txt"])

        assert result["success"] is True
        status = _git(["status", "--porcelain"], repo).stdout
        assert "?? staged2.txt" in status


# ---------------------------------------------------------------------------
# git_reset commit_hash argument validation
# ---------------------------------------------------------------------------


class TestGitResetRevisionValidation:
    def test_rejects_option_shaped_commit_hash(self, repo: Path):
        head_before = _rev_parse(repo, "HEAD")
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_reset(repo, mode="mixed", commit_hash="-x")

        assert _rev_parse(repo, "HEAD") == head_before, (
            "HEAD must never move -- git reset must never have run"
        )

    def test_accepts_head_tilde_1(self, repo: Path):
        head_tilde_1 = _rev_parse(repo, "HEAD~1")
        svc = _make_service()

        result = svc.git_reset(repo, mode="mixed", commit_hash="HEAD~1")

        assert result["success"] is True
        assert _rev_parse(repo, "HEAD") == head_tilde_1


class TestGitResetModeValidation:
    def test_rejects_hard_abbreviation_without_confirmation(self, repo: Path):
        """mode must be exactly one of the literal supported reset modes.
        A leading-dash check alone is not enough: git's own long-option
        parser accepts any unambiguous prefix of a long option name, so
        "har" -- which is neither dash-prefixed nor literally equal to
        "hard" -- would otherwise reach argv as `--har`, which git
        resolves to `--hard`, while never triggering the mode == "hard"
        confirmation-token branch that gates it."""
        head_before = _rev_parse(repo, "HEAD")
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_reset(repo, mode="har", commit_hash="HEAD~1")

        assert _rev_parse(repo, "HEAD") == head_before, (
            "HEAD must never move -- git reset must never have run"
        )

    def test_accepts_exact_mode_strings(self, repo: Path):
        svc = _make_service()
        for mode in ("soft", "mixed"):
            result = svc.git_reset(repo, mode=mode, commit_hash="HEAD")
            assert result["success"] is True


# ---------------------------------------------------------------------------
# merge_branch source_branch argument validation
# ---------------------------------------------------------------------------


class TestMergeBranchRevisionValidation:
    def test_rejects_option_shaped_source(self, repo: Path):
        head_before = _rev_parse(repo, "HEAD")
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.merge_branch(repo, source_branch="-Xtheirs")

        assert _rev_parse(repo, "HEAD") == head_before, (
            "HEAD must never move -- git merge must never have run"
        )

    def test_accepts_real_branch_name(self, repo: Path):
        svc = _make_service()

        result = svc.merge_branch(repo, source_branch="feature/nested")

        assert result["success"] is True
        assert (repo / "f.txt").read_text() == "one\ntwo\nthree\n"


# ---------------------------------------------------------------------------
# git_branch_create / git_branch_switch / git_branch_delete branch_name
# argument validation
# ---------------------------------------------------------------------------


class TestGitBranchNameValidation:
    def test_git_branch_create_rejects_leading_dash(self, repo: Path):
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_branch_create(repo, branch_name="--track")

        branches = _git(["branch"], repo).stdout
        assert "--track" not in branches

    def test_git_branch_create_accepts_slash_name(self, repo: Path):
        svc = _make_service()

        result = svc.git_branch_create(repo, branch_name="feature/another-thing")

        assert result["success"] is True
        branches = _git(["branch"], repo).stdout
        assert "feature/another-thing" in branches

    def test_git_branch_switch_rejects_force_flag_preserves_dirty_changes(
        self, repo: Path
    ):
        (repo / "f.txt").write_text("uncommitted local edit\n")
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_branch_switch(repo, branch_name="-f")

        assert (repo / "f.txt").read_text() == "uncommitted local edit\n", (
            "branch_name must be a well-formed ref name and must never "
            "start with '-' or '+'"
        )

    def test_git_branch_switch_accepts_slash_name(self, repo: Path):
        svc = _make_service()

        result = svc.git_branch_switch(repo, branch_name="feature/nested")

        assert result["success"] is True
        assert result["current_branch"] == "feature/nested"

    def test_git_branch_delete_rejects_leading_dash(self, repo: Path):
        svc = _make_service()
        first = svc.git_branch_delete(repo, branch_name="feature/nested")
        token = first["token"]

        with pytest.raises(GitArgumentValidationError):
            svc.git_branch_delete(repo, branch_name="-D", confirmation_token=token)

        branches = _git(["branch"], repo).stdout
        assert "feature/nested" in branches, (
            "feature/nested must remain -- git branch -d must never have run"
        )

    def test_git_branch_delete_accepts_valid_branch(self, repo: Path):
        svc = _make_service()
        svc.git_branch_create(repo, branch_name="to-delete")
        first = svc.git_branch_delete(repo, branch_name="to-delete")
        token = first["token"]

        result = svc.git_branch_delete(
            repo, branch_name="to-delete", confirmation_token=token
        )

        assert result["success"] is True
        branches = _git(["branch"], repo).stdout
        assert "to-delete" not in branches
