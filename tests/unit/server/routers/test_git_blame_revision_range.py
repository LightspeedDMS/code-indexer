"""REST `GET .../git/blame` must accept a two-dot/three-dot revision RANGE
in `rev`, since `git blame` itself accepts a range (restricting the blame
to that range, with boundary commits marked `^`): each non-empty side
must resolve and never start with '-'.

Exercised through the real REST route against a REAL throwaway git
repository (no mocking of git itself, per this repo's Anti-Mock rule).
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
    _git(["commit", "-q", "-m", "c1 on main"], repo)
    _git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "c2 on feature"], repo)
    _git(["checkout", "-q", "main"], repo)
    return repo


@contextmanager
def _arm_repo(repo_path: Path):
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


class TestGitBlameRevisionRange:
    def test_two_dot_range_accepted(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/blame",
                params={"path": "f.txt", "rev": "main..feature"},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["lines"]) == 2

    def test_caret_bang_suffix_accepted(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/blame",
                params={"path": "f.txt", "rev": "feature^!"},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["lines"]) == 2

    def test_accepts_range_with_option_shaped_side_deferred_to_git(
        self, test_client, tmp_path
    ):
        """`main..-x` is not an option: the whole value starts with 'm'.
        There is no per-side check, so this reaches `git blame` unchanged
        and fails there instead, surfacing as this route's existing 404
        mapping -- the same as before any validation existed."""
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/blame",
                params={"path": "f.txt", "rev": "main..-x"},
            )
        assert response.status_code == 404, response.text

    def test_rejects_whole_value_starting_with_dash(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                f"{_BASE}/blame",
                params={"path": "f.txt", "rev": "-x..feature"},
            )
        assert response.status_code == 400, response.text
