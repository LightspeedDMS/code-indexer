"""REST `POST .../git/stage` and `.../git/unstage` pass each file path to
git after a `--` separator, so a real file literally named `-` is staged
and unstaged like any other path. A NUL byte is never a legal path
character: `/unstage` (like `/stage`) answers it with HTTP 400 before any
git subprocess runs.

Exercised through the real REST routes against a REAL throwaway git
repository (no mocking of git itself, per this repo's Anti-Mock rule);
only the alias-to-path lookup is replaced.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)

_BASE = "/api/v1/repos/myrepo/git"


def _git(args: list, cwd: Path) -> str:
    out: str = subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout
    return out


def _staged(repo: Path) -> list:
    return _git(["diff", "--cached", "--name-only", "-z"], repo).split("\0")[:-1]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    _git(["init", "-q", "-b", "main", str(r)], tmp_path)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test User"], r)
    (r / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], r)
    _git(["commit", "-q", "-m", "init"], r)
    return r


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


@pytest.fixture()
def test_client(tmp_path):
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _access_service_admin,
    )

    user = Mock()
    user.username = "testuser"
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        with TestClient(app) as client:
            # After lifespan startup (it installs its own access service):
            # the caller is an admin of a real access service, so the
            # activated-repo guard passes; these tests pin route behaviour.
            with _access_service_admin(tmp_path / "access-groups.db", user.username):
                yield client
    finally:
        app.dependency_overrides.clear()


class TestStageUnstageDashFile:
    def test_stage_file_named_dash(self, test_client, repo: Path):
        (repo / "-").write_text("content\n")

        with _arm_repo(repo):
            response = test_client.post(f"{_BASE}/stage", json={"file_paths": ["-"]})

        assert response.status_code == 200, response.text
        assert response.json()["staged_files"] == ["-"]
        assert _staged(repo) == ["-"]

    def test_unstage_file_named_dash(self, test_client, repo: Path):
        (repo / "-").write_text("content\n")
        _git(["add", "--", "-"], repo)

        with _arm_repo(repo):
            response = test_client.post(f"{_BASE}/unstage", json={"file_paths": ["-"]})

        assert response.status_code == 200, response.text
        assert response.json()["unstaged_files"] == ["-"]
        assert _staged(repo) == []


class TestUnstageRejectsNul:
    def test_nul_entry_returns_400_and_leaves_index(self, test_client, repo: Path):
        (repo / "a.txt").write_text("content\n")
        _git(["add", "a.txt"], repo)

        with _arm_repo(repo):
            response = test_client.post(
                f"{_BASE}/unstage", json={"file_paths": ["a.txt", "a\x00b"]}
            )

        assert response.status_code == 400, response.text
        assert "NUL" in response.json()["detail"]
        assert _staged(repo) == ["a.txt"]
