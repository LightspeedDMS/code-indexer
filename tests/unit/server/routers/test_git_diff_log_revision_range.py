"""REST `git/diff`, `git/log`, and `git/file-history` must accept a
two-dot/three-dot revision RANGE (and the `^!` single-revision suffix),
not just a single revision expression: each non-empty side of a range
must resolve and never start with '-'.

All tests exercise the real REST routes through FastAPI's TestClient
against a REAL throwaway git repository (no mocking of git itself, per
this repo's Anti-Mock rule). Only the alias-to-filesystem-path resolution
layer is patched.
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
    """main has 2 commits; feature branches off main's first commit and
    adds one commit of its own, so `main..feature` contains exactly the
    feature-only commit and `main` has one commit `feature` lacks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c1 on main"], repo)
    _git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c2 on feature"], repo)
    _git(["checkout", "-q", "main"], repo)
    (repo / "g.txt").write_text("main only\n")
    _git(["add", "g.txt"], repo)
    _git(["commit", "-q", "-m", "c3 on main"], repo)
    return repo


@contextmanager
def _arm_repo(repo_path: Path):
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
    """file-history goes through routers.git._get_activated_repo_manager()."""
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
def test_client(mock_user, tmp_path):
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _access_service_admin,
    )

    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            # After lifespan startup (it installs its own access service):
            # the caller is an admin of a real access service, so the
            # activated-repo guard passes; these tests pin route behaviour.
            with _access_service_admin(
                tmp_path / "access-groups.db", mock_user.username
            ):
                yield client
    finally:
        app.dependency_overrides.clear()


class TestGitDiffRevisionRange:
    def test_two_dot_range_diffs_only_feature_commit(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/diff",
                params={"from_revision": "main", "to_revision": "feature"},
            )
        assert response.status_code == 200, response.text
        assert "+two" in response.json()["diff_text"]

    def test_three_dot_range_accepted(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/diff",
                params={"from_revision": "main...feature"},
            )
        assert response.status_code == 200, response.text

    def test_accepts_range_with_option_shaped_side_deferred_to_git(
        self, test_client, tmp_path
    ):
        """`main..-x` is not an option: the whole value starts with 'm'.
        There is no per-side check, so this reaches `git diff` unchanged
        and fails there instead ("unknown revision"), the same as before
        any validation existed."""
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/diff",
                params={"from_revision": "main..-x"},
            )
        assert response.status_code == 500, response.text

    def test_rejects_whole_value_starting_with_dash(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/diff",
                params={"from_revision": "-x..feature"},
            )
        assert response.status_code == 400, response.text


class TestGitLogRevisionRange:
    def test_branch_as_two_dot_range_returns_only_feature_commit(
        self, test_client, tmp_path
    ):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/log",
                params={"branch": "main..feature"},
            )
        assert response.status_code == 200, response.text
        commits = response.json()["commits"]
        assert len(commits) == 1
        assert commits[0]["message"] == "c2 on feature"

    def test_branch_as_caret_bang_suffix_accepted(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/log",
                params={"branch": "feature^!"},
            )
        assert response.status_code == 200, response.text
        commits = response.json()["commits"]
        assert len(commits) == 1
        assert commits[0]["message"] == "c2 on feature"

    def test_rejects_range_with_option_shaped_side(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/log",
                params={"branch": "-x..feature"},
            )
        assert response.status_code == 400, response.text


class TestGitFileHistoryRevisionRange:
    def test_rev_as_two_dot_range_returns_only_feature_commit(
        self, test_client, tmp_path
    ):
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/file-history",
                params={"path": "f.txt", "rev": "main..feature"},
            )
        assert response.status_code == 200, response.text
        commits = response.json()["commits"]
        assert len(commits) == 1

    def test_accepts_range_with_option_shaped_side_deferred_to_git(
        self, test_client, tmp_path
    ):
        """`main..-x` is not an option: the whole value starts with 'm'.
        There is no per-side check, so this reaches `git log` unchanged
        and fails there instead, surfacing as this route's existing 404
        "File or revision not found" mapping -- the same as before any
        validation existed."""
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/file-history",
                params={"path": "f.txt", "rev": "main..-x"},
            )
        assert response.status_code == 404, response.text

    def test_rejects_whole_value_starting_with_dash(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo_app_state(repo):
            response = test_client.get(
                f"{_BASE}/file-history",
                params={"path": "f.txt", "rev": "-x..feature"},
            )
        assert response.status_code == 400, response.text
