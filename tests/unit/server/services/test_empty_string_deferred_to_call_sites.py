"""An empty-string argument (branch/rev/remote/commit_hash) is not one of
the two hazards `git_argv_safety.py` checks (a leading '-', or a NUL/CR/
LF/other C0 control character): an empty argv element can never be read
as a git OPTION. `validate_revision`, `validate_revision_range`,
`validate_branch_name`, and `validate_remote_name` now all pass an empty
string through unchanged, so each call site's own pre-existing truthy
check (e.g. `if branch:`) or git itself decides what an empty value
means -- exactly as it did with no validation at all, before these
validators existed.

This file pins that outcome at each affected call site, through
the REAL front door (REST dispatch or the real service method) against
REAL throwaway git repositories (no mocking of git itself, per this
repo's Anti-Mock rule). Every outcome asserted here was verified either
empirically against real git before being written.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)

_BASE = "/api/v1/repos/myrepo/git"


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c1"], repo)
    return repo


# ---------------------------------------------------------------------------
# REST front door: log / file-history / cat / blame
# ---------------------------------------------------------------------------


@contextmanager
def _arm_repo(repo_path: Path):
    """git_log goes through git_operations_service.activated_repo_manager."""
    mock_arm = Mock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    original = git_operations_service.activated_repo_manager
    git_operations_service.activated_repo_manager = mock_arm
    try:
        yield mock_arm
    finally:
        git_operations_service.activated_repo_manager = original


@contextmanager
def _arm_repo_app_state(repo_path: Path):
    """cat/blame/file-history go through routers.git._get_activated_repo_manager()."""
    with patch(
        "code_indexer.server.routers.git._get_activated_repo_manager"
    ) as mock_getter:
        mock_arm = Mock()
        mock_arm.get_activated_repo_path.return_value = str(repo_path)
        mock_getter.return_value = mock_arm
        yield mock_arm


@pytest.fixture()
def mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture()
def test_client(mock_user):
    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestGitLogEmptyBranchRestFrontDoor:
    def test_empty_branch_returns_200_head_log(self, test_client, tmp_path):
        """`?branch=` sends branch="" (not omitted), which reaches
        `git_log`'s own pre-existing `rev_spec = branch if branch else
        "HEAD"` truthy fallback unchanged, exactly as it did before
        `validate_revision_range` existed."""
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(f"{_BASE}/log", params={"branch": ""})
        assert response.status_code == 200, response.text
        assert len(response.json()["commits"]) == 1


class TestGitFileHistoryEmptyRevRestFrontDoor:
    def test_empty_rev_returns_200(self, test_client, tmp_path):
        """`?rev=` sends rev="" (not omitted), which reaches this route's
        own pre-existing `if rev: cmd.append(rev)` truthy check
        unchanged."""
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/file-history",
                params={"path": "f.txt", "rev": ""},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["commits"]) == 1


class TestGitCatEmptyRevRestFrontDoor:
    def test_empty_rev_returns_404_not_200(self, test_client, tmp_path):
        """`rev` here is a plain `str` with Query default "HEAD" (never
        None), so rev="" reaches `git rev-parse ""` unchanged -- which
        fails on real git -- instead of being coerced to "HEAD"."""
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/cat",
                params={"path": "f.txt", "rev": ""},
            )
        assert response.status_code == 404, response.text


class TestGitBlameEmptyRevRestFrontDoor:
    def test_empty_rev_returns_404_not_200(self, test_client, tmp_path):
        """Same reasoning as git/cat above: rev="" reaches `git blame
        --porcelain ""` unchanged, which fails on real git."""
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/blame",
                params={"path": "f.txt", "rev": ""},
            )
        assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Server GitOperationsService: git_log / git_diff / git_reset / git_fetch /
# git_push / git_pull
# ---------------------------------------------------------------------------


class TestServiceGitLogEmptyBranch:
    def test_empty_branch_matches_none_branch(self, tmp_path):
        repo = _make_repo(tmp_path)
        with_none = git_operations_service.git_log(repo, branch=None)
        with_empty = git_operations_service.git_log(repo, branch="")
        assert with_empty["commits"] == with_none["commits"]
        assert len(with_empty["commits"]) == 1


class TestServiceGitDiffEmptyRevision:
    def test_empty_from_revision_matches_none(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "f.txt").write_text("one\ntwo\n")
        with_none = git_operations_service.git_diff(repo, from_revision=None)
        with_empty = git_operations_service.git_diff(repo, from_revision="")
        assert with_empty["diff_text"] == with_none["diff_text"]


class TestServiceGitResetEmptyCommitHash:
    def test_empty_commit_hash_defaults_to_head(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = git_operations_service.git_reset(repo, mode="mixed", commit_hash="")
        assert result["success"] is True
        assert result["target_commit"] == "HEAD"


class TestServiceGitFetchEmptyRemote:
    def test_empty_remote_does_not_raise_validation_error(self, tmp_path):
        """`git fetch --end-of-options ""` itself succeeds on real git
        (verified empirically), and `validate_remote_name` no longer
        rejects the empty string before that subprocess ever runs."""
        repo = _make_repo(tmp_path)
        result = git_operations_service.git_fetch(repo, remote="")
        assert result["success"] is True


class TestServiceGitPushEmptyBranch:
    def test_empty_branch_pushes_current_branch(self, tmp_path):
        """branch="" is falsy, so `git_push`'s own pre-existing `if
        branch: cmd.append(branch)` check omits it from argv entirely --
        the same as branch=None -- and `push.default=current` pushes the
        current branch with no explicit branch argument."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git(["init", "-q", "--bare"], bare)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-q", "-b", "main"], repo)
        _git(["config", "user.email", "test@example.com"], repo)
        _git(["config", "user.name", "Test User"], repo)
        _git(["config", "push.default", "current"], repo)
        _git(["remote", "add", "origin", str(bare)], repo)
        (repo / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-q", "-m", "c1"], repo)

        result = git_operations_service.git_push(repo, remote="origin", branch="")
        assert result["success"] is True


class TestServiceGitPullEmptyBranch:
    def test_empty_branch_pulls_normally(self, tmp_path):
        """Same reasoning as push above: branch="" is omitted from argv
        by `git_pull`'s own pre-existing `if branch:` check, so `git pull
        --end-of-options origin` (no branch positional) runs, exactly as
        branch=None would."""
        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git(["init", "-q", "--bare"], bare)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-q", "-b", "main"], repo)
        _git(["config", "user.email", "test@example.com"], repo)
        _git(["config", "user.name", "Test User"], repo)
        _git(["remote", "add", "origin", str(bare)], repo)
        (repo / "f.txt").write_text("one\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-q", "-m", "c1"], repo)
        _git(["push", "-q", "-u", "origin", "main"], repo)

        result = git_operations_service.git_pull(repo, remote="origin", branch="")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# global_repos.GitOperationsService: get_log / get_blame / show_commit /
# get_file_at_revision
# ---------------------------------------------------------------------------


class TestGlobalGetLogEmptyBranch:
    def test_empty_branch_matches_none_branch(self, tmp_path):
        from code_indexer.global_repos.git_operations import GitOperationsService

        repo = _make_repo(tmp_path)
        service = GitOperationsService(repo)
        with_none = service.get_log(branch=None)
        with_empty = service.get_log(branch="")
        assert with_empty.commits == with_none.commits
        assert len(with_empty.commits) == 1


class TestGlobalGetBlameEmptyRevision:
    def test_empty_revision_matches_none_revision(self, tmp_path):
        from code_indexer.global_repos.git_operations import (
            BlameResult,
            GitOperationsService,
        )

        repo = _make_repo(tmp_path)
        service = GitOperationsService(repo)
        with_none = service.get_blame(path="f.txt", revision=None)
        with_empty = service.get_blame(path="f.txt", revision="")
        assert isinstance(with_none, BlameResult)
        assert isinstance(with_empty, BlameResult)
        assert with_empty.lines == with_none.lines
        assert len(with_empty.lines) == 1


class TestShowCommitEmptyCommitHash:
    def test_empty_commit_hash_raises_original_message(self, tmp_path):
        """`validate_revision` never resolves commit_hash itself, so an
        empty string reaches this method's OWN internal `git rev-parse`
        call unchanged; that call fails on real git (`git rev-parse ""`
        exits 128, verified empirically), raising the exact pre-existing
        message this method has always raised."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        repo = _make_repo(tmp_path)
        service = GitOperationsService(repo)
        with pytest.raises(ValueError, match=r"^Commit not found: $"):
            service.show_commit(commit_hash="")


class TestFileAtRevisionEmptyRevision:
    def test_empty_revision_raises_original_message(self, tmp_path):
        """Same reasoning as show_commit above: an empty revision reaches
        this method's OWN internal `git rev-parse --verify` call
        unchanged; `git rev-parse --verify "^{commit}"` itself fails on
        real git (verified empirically), raising the exact pre-existing
        message."""
        from code_indexer.global_repos.git_operations import GitOperationsService

        repo = _make_repo(tmp_path)
        service = GitOperationsService(repo)
        with pytest.raises(ValueError, match=r"^Invalid revision: $"):
            service.get_file_at_revision(path="f.txt", revision="")
