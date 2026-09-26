"""Integration tests for the shared git-argv validation wired into
`GitOperationsService.git_push` / `git_pull` / `git_fetch` / `git_diff`.

All tests exercise those methods directly against REAL throwaway git
repositories (no mocking of git itself, per this repo's Anti-Mock rule).
Each repo carries a local-path `golden` remote, matching how activated
repos are actually configured in this project.

Rejection tests assert BOTH that a validation exception is raised AND
that the caller-supplied value never occupied an option position: the
marker file that option would create never appears. An error surfacing
afterward is not enough on its own.

Legitimate-usage tests prove the fix does not break real push/pull/fetch to
a local-path remote, nor real git_diff usage with revisions/file_paths/path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_argv_safety import GitArgumentValidationError
from code_indexer.server.services.git_operations_service import (
    GitOperationsService,
    git_operations_service,
)


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_service() -> GitOperationsService:
    """The module's real singleton, mirroring
    tests/unit/server/routers/test_git_read_endpoints_contract.py's pattern.

    Unlike `GitOperationsService.__new__(GitOperationsService)` (used by
    tests that only exercise pure-Python helpers like
    `_count_pushed_commits`), git_push/pull/fetch/diff read
    `self._git_timeouts`/`self._api_limits`, which are lazily resolved from
    `_git_timeouts_lazy`/`_api_limits_lazy` -- attributes that only exist
    after `__init__` runs. The singleton is already fully constructed (Bug
    #1650 lazy init resolves cleanly to config defaults in this dev
    environment, exactly as production does) and takes repo_path
    explicitly, so no ActivatedRepoManager/config bootstrapping is
    exercised by these tests.
    """
    return git_operations_service


@pytest.fixture
def repo_with_golden_remote(tmp_path: Path) -> Path:
    """Real repo with a commit, on branch 'main', with upstream tracking set
    on a local-path 'golden' remote -- matching how activated repos are
    always configured in this project."""
    remote = tmp_path / "golden.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], cwd=remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    _git(["checkout", "-q", "-b", "main"], cwd=repo)
    (repo / "f.txt").write_text("hello\n")
    _git(["add", "f.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    _git(["remote", "add", "golden", str(remote)], cwd=repo)
    _git(["push", "-q", "--set-upstream", "golden", "main"], cwd=repo)

    return repo


# ---------------------------------------------------------------------------
# git_push / git_pull / git_fetch remote/branch argument validation
# ---------------------------------------------------------------------------


class TestRemoteArgumentValidation:
    def test_git_push_rejects_receive_pack_option_injection(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        """remote='--receive-pack=<cmd>' must be rejected before argv
        construction, and the command named in it must never run."""
        marker = tmp_path / "push_marker"
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_push(
                repo_with_golden_remote,
                remote=f"--receive-pack=touch {marker}",
                branch="golden",
            )

        assert not marker.exists(), (
            "git_push must never execute an option value passed as `remote`"
        )

    def test_git_pull_rejects_upload_pack_option_injection(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        marker = tmp_path / "pull_marker"
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_pull(
                repo_with_golden_remote,
                remote=f"--upload-pack=touch {marker}",
                branch="golden",
            )

        assert not marker.exists(), (
            "git_pull must never execute an option value passed as `remote`"
        )

    def test_git_fetch_rejects_upload_pack_option_injection(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        marker = tmp_path / "fetch_marker"
        svc = _make_service()

        with pytest.raises(GitArgumentValidationError):
            svc.git_fetch(
                repo_with_golden_remote,
                remote=f"--upload-pack=touch {marker}",
            )

        assert not marker.exists(), (
            "git_fetch must never execute an option value passed as `remote`"
        )

    def test_git_push_rejects_remote_not_a_configured_remote(
        self, repo_with_golden_remote: Path
    ):
        """Even a syntactically clean remote name must be one the repo
        actually has configured."""
        svc = _make_service()
        with pytest.raises(GitArgumentValidationError):
            svc.git_push(
                repo_with_golden_remote, remote="not-a-real-remote", branch="main"
            )

    def test_git_push_rejects_branch_with_leading_dash(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        with pytest.raises(GitArgumentValidationError):
            svc.git_push(
                repo_with_golden_remote, remote="golden", branch="--force-with-lease"
            )

    # -- Legitimate usage must keep working -----------------------------

    def test_git_push_legitimate_local_remote_succeeds(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        """Prove the fix does not break real push to a local-path remote
        (a real precondition of golden-repo sync in this project)."""
        repo = repo_with_golden_remote
        (repo / "f.txt").write_text("hello again\n")
        _git(["add", "f.txt"], cwd=repo)
        _git(["commit", "-q", "-m", "second"], cwd=repo)

        svc = _make_service()
        result = svc.git_push(repo, remote="golden", branch="main")

        assert result["success"] is True
        assert result["pushed_commits"] >= 1

    def test_git_fetch_legitimate_local_remote_succeeds(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        result = svc.git_fetch(repo_with_golden_remote, remote="golden")
        assert result["success"] is True

    def test_git_pull_legitimate_local_remote_succeeds(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        result = svc.git_pull(repo_with_golden_remote, remote="golden", branch="main")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# git_push_with_pat is a SEPARATE argv-building path from git_push() above --
# the MCP git_push handler calls it directly. It must validate remote/branch
# itself, before ANY subprocess (including its own "get remote URL"
# preflight and the upstream-tracking `git branch --set-upstream-to=...`
# call, which takes `branch` as a bare positional argument), so no caller --
# present or future -- can bypass validation by using this entry point
# instead of git_push().
# ---------------------------------------------------------------------------


class TestPushWithPatArgumentValidation:
    """git_push_with_pat must call validate_remote_name/validate_branch_name
    itself, before any subprocess (including its own get-url preflight)."""

    def test_git_push_with_pat_rejects_receive_pack_option_injection(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        marker = tmp_path / "push_with_pat_marker"
        svc = _make_service()
        credential = {"token": "unused-fixture-token-rejected-before-any-auth"}

        with pytest.raises(GitArgumentValidationError):
            svc.git_push_with_pat(
                repo_with_golden_remote,
                remote=f"--receive-pack=touch {marker}",
                branch="main",
                credential=credential,
            )

        assert not marker.exists(), (
            "git_push_with_pat must never execute an option value passed as `remote`"
        )

    def test_git_push_with_pat_rejects_branch_with_leading_dash(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        credential = {"token": "unused-fixture-token-rejected-before-any-auth"}
        with pytest.raises(GitArgumentValidationError):
            svc.git_push_with_pat(
                repo_with_golden_remote,
                remote="golden",
                branch="--force",
                credential=credential,
            )

    def test_git_push_with_pat_rejects_remote_not_configured(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        credential = {"token": "unused-fixture-token-rejected-before-any-auth"}
        with pytest.raises(GitArgumentValidationError):
            svc.git_push_with_pat(
                repo_with_golden_remote,
                remote="not-a-real-remote",
                branch="main",
                credential=credential,
            )


# ---------------------------------------------------------------------------
# `--end-of-options` is not a hard argv boundary for `git pull` on every git
# version -- pull delegates to an internal fetch+merge step whose own
# argument handling does not always inherit the boundary set at the
# top-level `pull` invocation. This is proven here by calling raw git
# directly (bypassing GitOperationsService, which already rejects this via
# validate_remote_name before argv is ever built) -- documenting exactly why
# remote-membership validation, not the separator, is the real control for
# pull. push/fetch do NOT share this weakness on the same git version
# (proven below). `git diff` has its own, separate `--end-of-options`
# weakness: diff's `--no-index` pre-scan still recognizes `--no-index` even
# after `--end-of-options` -- the real control for diff is the leading-'-'
# rejection in validate_revision/validate_pathspecs (see
# TestDiffArgumentValidation below and the matching comment in
# git_operations_service.git_diff), not --end-of-options.
# ---------------------------------------------------------------------------


class TestEndOfOptionsBoundaryByGitCommand:
    """These tests exercise raw `git` directly, not GitOperationsService --
    they document upstream git behavior that the validators above must
    never be relaxed to rely on."""

    def _real_repo_with_golden_remote(self, tmp_path: Path) -> Path:
        remote = tmp_path / "golden.git"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", "--bare"], cwd=str(remote), check=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"], cwd=str(repo), check=True
        )
        subprocess.run(
            ["git", "checkout", "-q", "-b", "main"], cwd=str(repo), check=True
        )
        (repo / "f.txt").write_text("hi\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "remote", "add", "golden", str(remote)], cwd=str(repo), check=True
        )
        subprocess.run(
            ["git", "push", "-q", "--set-upstream", "golden", "main"],
            cwd=str(repo),
            check=True,
        )
        return repo

    def test_end_of_options_alone_is_insufficient_for_pull(self, tmp_path: Path):
        """Documents a real git behavior, not a code bug: `git pull
        --end-of-options '--upload-pack=<cmd>' <branch>` still executes
        <cmd> on git 2.52, because pull's own option-boundary does not
        propagate into its internal fetch delegation. If this assertion
        ever starts FAILING, a future git version has closed this gap for
        pull -- good news, but validate_remote_name() must stay regardless,
        since it is the only control proven to hold across git versions.
        """
        repo = self._real_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "pull_end_of_options_marker.txt"

        subprocess.run(
            [
                "git",
                "pull",
                "--end-of-options",
                f"--upload-pack=touch {marker}",
                "main",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

        assert marker.exists(), (
            "Expected git pull --end-of-options alone to be insufficient "
            "(git 2.52 known behavior) -- if this now fails, see the "
            "docstring above before removing any validation."
        )

    def test_end_of_options_alone_blocks_push_injection(self, tmp_path: Path):
        """Unlike pull, `git push --end-of-options` alone DOES block an
        option value passed where a plain remote value is expected
        (verified on git 2.52) -- kept here so a future git version
        regressing this is caught, not assumed."""
        repo = self._real_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "push_end_of_options_marker.txt"

        subprocess.run(
            [
                "git",
                "push",
                "--end-of-options",
                f"--receive-pack=touch {marker}",
                "golden",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

        assert not marker.exists(), (
            "git push --end-of-options unexpectedly failed to block the "
            "option value on this git version"
        )

    def test_end_of_options_alone_blocks_fetch_injection(self, tmp_path: Path):
        """Unlike pull, `git fetch --end-of-options` alone DOES block an
        option value passed where a plain remote value is expected
        (verified on git 2.52) -- kept here so a future git version
        regressing this is caught, not assumed."""
        repo = self._real_repo_with_golden_remote(tmp_path)
        marker = tmp_path / "fetch_end_of_options_marker.txt"

        subprocess.run(
            [
                "git",
                "fetch",
                "--end-of-options",
                f"--upload-pack=touch {marker}",
                "golden",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

        assert not marker.exists(), (
            "git fetch --end-of-options unexpectedly failed to block the "
            "option value on this git version"
        )


# ---------------------------------------------------------------------------
# git_diff from_revision/to_revision/file_paths argument validation
# ---------------------------------------------------------------------------


class TestDiffArgumentValidation:
    def test_git_diff_rejects_no_index_file_paths_output_combo(
        self, tmp_path: Path, repo_with_golden_remote: Path
    ):
        """from_revision='--no-index' plus a file_paths list crafted to
        redirect output via --output= must be rejected before argv
        construction, and no output file must ever be created anywhere
        (repo cwd or tmp_path)."""
        decoy = tmp_path / "decoy_secret.txt"
        decoy.write_text("decoy test content\n")
        output_marker = repo_with_golden_remote / "diff_output_marker.txt"

        svc = _make_service()
        with pytest.raises(GitArgumentValidationError):
            svc.git_diff(
                repo_with_golden_remote,
                from_revision="--no-index",
                file_paths=[
                    "--output=diff_output_marker.txt",
                    "--text",
                    str(decoy),
                    "/dev/null",
                ],
            )

        assert not output_marker.exists(), (
            "git diff must never write a file from an --output= value "
            "passed as a file_paths entry"
        )

    def test_git_diff_rejects_file_paths_entry_with_leading_dash(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        with pytest.raises(GitArgumentValidationError):
            svc.git_diff(repo_with_golden_remote, file_paths=["f.txt", "-x"])

    def test_git_diff_rejects_to_revision_with_leading_dash(
        self, repo_with_golden_remote: Path
    ):
        svc = _make_service()
        with pytest.raises(GitArgumentValidationError):
            svc.git_diff(
                repo_with_golden_remote,
                from_revision="HEAD",
                to_revision="--output=diff_output_marker.txt",
            )

    # -- Legitimate usage must keep working -----------------------------

    def test_git_diff_legitimate_with_revisions_file_paths_and_path(
        self, repo_with_golden_remote: Path
    ):
        """Prove the fix does not break real git_diff usage combining
        revisions, the legacy file_paths list, and the path filter."""
        repo = repo_with_golden_remote
        sha0 = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        (repo / "f.txt").write_text("hello changed\n")
        (repo / "g.txt").write_text("new file\n")
        _git(["add", "f.txt", "g.txt"], cwd=repo)
        _git(["commit", "-q", "-m", "second"], cwd=repo)
        sha1 = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()

        svc = _make_service()

        # revisions only
        result = svc.git_diff(repo, from_revision=sha0, to_revision=sha1)
        assert result["files_changed"] == 2

        # legacy file_paths list restricts to one file
        result = svc.git_diff(
            repo, from_revision=sha0, to_revision=sha1, file_paths=["f.txt"]
        )
        assert "f.txt" in result["diff_text"]
        assert "g.txt" not in result["diff_text"]

        # path param restricts to one file
        result = svc.git_diff(repo, from_revision=sha0, to_revision=sha1, path="g.txt")
        assert "g.txt" in result["diff_text"]
        assert "f.txt" not in result["diff_text"]

    def test_git_diff_legitimate_no_revision_working_tree_diff(
        self, repo_with_golden_remote: Path
    ):
        """No from_revision at all (plain working-tree-vs-index diff) must
        still work after adding --end-of-options."""
        repo = repo_with_golden_remote
        (repo / "f.txt").write_text("uncommitted change\n")

        svc = _make_service()
        result = svc.git_diff(repo)
        assert result["files_changed"] >= 1
