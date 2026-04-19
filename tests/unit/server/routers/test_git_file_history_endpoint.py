"""
Unit tests for GET /api/v1/repos/{alias}/git/file-history endpoint.

Bug #824 — missing git file-history handler.

Uses the shared `git_repo` fixture from conftest.py.

Tests:
  test_git_file_history_happy_path                       -- 200 with commits list
  test_git_file_history_path_traversal_returns_400       -- 400 on path with ".."
  test_git_file_history_absolute_path_returns_400        -- 400 on absolute path
  test_git_file_history_unknown_alias_returns_404        -- 404 when alias not activated
  test_git_file_history_nonexistent_file_returns_empty   -- 200 + empty list (git log exits 0)
  test_git_file_history_no_auth_returns_401_or_403       -- 401 or 403 without auth
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user

# Required keys in each commit entry in the file-history response
_COMMIT_KEYS = {"commit_hash", "author", "date", "message"}


# ---------------------------------------------------------------------------
# Patching helpers
# ---------------------------------------------------------------------------


@contextmanager
def _arm_repo(repo_path: Path):
    """Patch git router's activated_repo_manager to return repo_path."""
    with patch(
        "code_indexer.server.routers.git.activated_repo_manager"
    ) as mock_arm:
        mock_arm.get_activated_repo_path.return_value = str(repo_path)
        yield mock_arm


@contextmanager
def _arm_not_found():
    """Patch git router's activated_repo_manager to simulate alias not activated."""
    with patch(
        "code_indexer.server.routers.git.activated_repo_manager"
    ) as mock_arm:
        mock_arm.get_activated_repo_path.side_effect = FileNotFoundError("not activated")
        yield mock_arm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture()
def test_client(mock_user):
    """Function-scoped client with guaranteed override cleanup."""
    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests (git_repo fixture provided by conftest.py)
# ---------------------------------------------------------------------------


def test_git_file_history_happy_path(test_client, git_repo):
    """GET /api/v1/repos/{alias}/git/file-history returns 200 with commits list."""
    repo, head_sha = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/file-history?path=hello.txt"
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert "commits" in data
    commits = data["commits"]
    assert isinstance(commits, list)
    assert len(commits) >= 1

    commit = commits[0]
    assert set(commit.keys()) == _COMMIT_KEYS, (
        f"Commit keys {set(commit.keys())} differ from expected {_COMMIT_KEYS}"
    )
    assert len(commit["commit_hash"]) == 40
    assert commit["commit_hash"] == head_sha
    assert isinstance(commit["author"], str)
    assert isinstance(commit["date"], str)
    assert isinstance(commit["message"], str)


def test_git_file_history_path_traversal_returns_400(test_client, git_repo):
    """GET git/file-history with path containing '..' returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/file-history?path=../etc/passwd"
        )

    assert response.status_code == 400


def test_git_file_history_absolute_path_returns_400(test_client, git_repo):
    """GET git/file-history with absolute path returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/file-history?path=/etc/passwd"
        )

    assert response.status_code == 400


def test_git_file_history_unknown_alias_returns_404(test_client):
    """GET git/file-history returns 404 when alias is not activated."""
    with _arm_not_found():
        response = test_client.get(
            "/api/v1/repos/no_such_repo/git/file-history?path=hello.txt"
        )

    assert response.status_code == 404


def test_git_file_history_nonexistent_file_returns_empty(test_client, git_repo):
    """GET git/file-history for a non-existent path returns 200 + empty commits list.

    `git log --follow -- nonexistent` exits 0 with empty output when no commits
    touch the given path.  The handler returns 200 with an empty commits list
    rather than 404, which is consistent with git's own behavior.
    """
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/file-history?path=nonexistent.txt"
        )

    assert response.status_code == 200
    data = response.json()
    assert data["commits"] == []


def test_git_file_history_no_auth_returns_401_or_403():
    """GET /api/v1/repos/{alias}/git/file-history without auth returns 401 or 403."""
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/repos/somerepo/git/file-history?path=hello.txt"
            )
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.update(previous_overrides)
