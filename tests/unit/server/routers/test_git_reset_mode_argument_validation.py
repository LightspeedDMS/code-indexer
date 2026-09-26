"""REST `POST .../git/reset` must validate `mode` against the exact same
literal allowlist the MCP `git_reset` tool already enforces, before any
confirmation-token logic runs.

A leading-dash check alone would not be enough here: git's own long-option
parser accepts any unambiguous PREFIX of a long option name (verified
empirically against real git 2.52: `git reset --har` performs a real hard
reset, resolved as an abbreviation of `--hard`). A caller value like
"har" is neither dash-prefixed nor literally equal to the string "hard",
so a bare `mode == "hard"` comparison never routes it through the
confirmation-token gate -- yet it still reaches git as a real hard reset
once `f"--{mode}"` is built. Exercised through the real REST route against
a REAL throwaway git repository (no mocking of git itself, per this
repo's Anti-Mock rule).
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


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["checkout", "-q", "-b", "main"], repo)
    (repo / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "first"], repo)
    (repo / "f.txt").write_text("one\ntwo\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "second"], repo)
    return repo


@contextmanager
def _arm_repo(repo_path: Path):
    mock_arm = Mock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    original_arm = git_operations_service.activated_repo_manager
    git_operations_service.activated_repo_manager = mock_arm
    try:
        yield mock_arm
    finally:
        git_operations_service.activated_repo_manager = original_arm


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


class TestGitResetModeValidation:
    def test_rejects_hard_abbreviation_without_confirmation(
        self, test_client, tmp_path
    ):
        repo = _make_repo(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with _arm_repo(repo):
            response = test_client.post(
                "/api/v1/repos/myrepo/git/reset",
                json={"mode": "har", "commit_hash": "HEAD~1"},
            )

        assert response.status_code == 400, response.text
        assert "must be exactly one of" in response.text
        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        assert head_after == head_before, (
            "HEAD must never move -- git reset must never have run"
        )

    def test_accepts_exact_mode_string(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        head_tilde_1 = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()

        with _arm_repo(repo):
            response = test_client.post(
                "/api/v1/repos/myrepo/git/reset",
                json={"mode": "mixed", "commit_hash": "HEAD~1"},
            )

        assert response.status_code == 200, response.text
        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        assert head_after == head_tilde_1
