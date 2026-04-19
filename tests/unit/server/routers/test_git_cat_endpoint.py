"""
Unit tests for GET /api/v1/repos/{alias}/git/cat endpoint.

Bug #824 — missing git cat handler.

Tests:
  test_git_cat_happy_path                    -- 200 with content/path/size/rev keys
  test_git_cat_path_traversal_returns_400    -- 400 on path containing ".."
  test_git_cat_absolute_path_returns_400     -- 400 on absolute path
  test_git_cat_unknown_alias_returns_404     -- 404 when alias not activated
  test_git_cat_file_not_found_returns_404    -- 404 when file/rev not found in git
  test_git_cat_no_auth_returns_401_or_403    -- 401 or 403 without auth
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user

# Expected keys in the git/cat response
_EXPECTED_KEYS = {"content", "path", "size", "rev"}


# ---------------------------------------------------------------------------
# Helpers
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
# Tests
# ---------------------------------------------------------------------------


def test_git_cat_happy_path(test_client, git_repo):
    """GET /api/v1/repos/{alias}/git/cat returns 200 with correct shape."""
    repo, head_sha = git_repo

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/cat?path=hello.txt")

    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data.keys()) == _EXPECTED_KEYS, (
        f"Response keys {set(data.keys())} differ from expected {_EXPECTED_KEYS}"
    )
    assert data["path"] == "hello.txt"
    assert "hello world" in data["content"]
    assert isinstance(data["size"], int)
    assert data["size"] > 0
    # rev must be the resolved HEAD SHA (40 hex chars)
    assert len(data["rev"]) == 40
    assert data["rev"] == head_sha


def test_git_cat_path_traversal_returns_400(test_client, git_repo):
    """GET git/cat with path containing '..' returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/cat?path=../etc/passwd"
        )

    assert response.status_code == 400


def test_git_cat_absolute_path_returns_400(test_client, git_repo):
    """GET git/cat with absolute path returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/cat?path=/etc/passwd"
        )

    assert response.status_code == 400


def test_git_cat_unknown_alias_returns_404(test_client):
    """GET git/cat returns 404 when alias is not activated."""
    with _arm_not_found():
        response = test_client.get(
            "/api/v1/repos/no_such_repo/git/cat?path=hello.txt"
        )

    assert response.status_code == 404


def test_git_cat_file_not_found_returns_404(test_client, git_repo):
    """GET git/cat for non-existent file returns 404."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/cat?path=nonexistent.txt"
        )

    assert response.status_code == 404


def test_git_cat_no_auth_returns_401_or_403():
    """GET /api/v1/repos/{alias}/git/cat without auth returns 401 or 403."""
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/repos/somerepo/git/cat?path=hello.txt")
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.update(previous_overrides)
