"""REST `POST .../git/reset` must accept `mode="keep"` and `mode="merge"`
(the exact literal git reset modes, alongside soft/mixed/hard), while
still rejecting anything not an exact match (e.g. the unambiguous
abbreviation "har").

Exercised through the real REST route against a REAL throwaway git
repository (no mocking of git itself, per this repo's Anti-Mock rule).
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


class TestGitResetKeepAndMergeModes:
    def test_keep_mode_accepted_and_moves_head(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        head_tilde_1 = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()

        with _arm_repo(repo):
            response = test_client.post(
                "/api/v1/repos/myrepo/git/reset",
                json={"mode": "keep", "commit_hash": "HEAD~1"},
            )

        assert response.status_code == 200, response.text
        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        assert head_after == head_tilde_1

    def test_merge_mode_accepted_and_moves_head(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        head_tilde_1 = _git(["rev-parse", "HEAD~1"], repo).stdout.strip()

        with _arm_repo(repo):
            response = test_client.post(
                "/api/v1/repos/myrepo/git/reset",
                json={"mode": "merge", "commit_hash": "HEAD~1"},
            )

        assert response.status_code == 200, response.text
        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        assert head_after == head_tilde_1

    def test_abbreviation_still_rejected(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with _arm_repo(repo):
            response = test_client.post(
                "/api/v1/repos/myrepo/git/reset",
                json={"mode": "har", "commit_hash": "HEAD~1"},
            )

        assert response.status_code == 400, response.text
        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        assert head_after == head_before
