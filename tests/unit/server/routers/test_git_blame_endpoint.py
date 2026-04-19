"""
Unit tests for GET /api/v1/repos/{alias}/git/blame endpoint.

Bug #824 — missing git blame handler.

Uses the shared `git_repo` fixture from conftest.py which provides a real git
repository with hello.txt committed.  Repo creation helpers are NOT defined
here — they live in conftest.py.  The `_arm_repo` / `_arm_not_found` helpers
patch the activated_repo_manager on the git router, following the same pattern
as test_git_cat_endpoint.py.

Tests:
  test_git_blame_happy_path                  -- 200 with lines list (all required fields)
  test_git_blame_path_traversal_returns_400  -- 400 on path with ".."
  test_git_blame_absolute_path_returns_400   -- 400 on absolute path
  test_git_blame_unknown_alias_returns_404   -- 404 when alias not activated
  test_git_blame_file_not_found_returns_404  -- 404 when file not in repo
  test_git_blame_no_auth_returns_401_or_403  -- 401 or 403 without auth
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user

# Required keys in each blame line entry
_LINE_KEYS = {"line_number", "commit_hash", "author", "date", "content"}


# ---------------------------------------------------------------------------
# Patching helpers (not repo-creation helpers)
# ---------------------------------------------------------------------------


@contextmanager
def _arm_repo(repo_path: Path):
    """Patch git router's activated_repo_manager to return repo_path."""
    with patch("code_indexer.server.routers.git.activated_repo_manager") as mock_arm:
        mock_arm.get_activated_repo_path.return_value = str(repo_path)
        yield mock_arm


@contextmanager
def _arm_not_found():
    """Patch git router's activated_repo_manager to simulate alias not activated."""
    with patch("code_indexer.server.routers.git.activated_repo_manager") as mock_arm:
        mock_arm.get_activated_repo_path.side_effect = FileNotFoundError(
            "not activated"
        )
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


def test_git_blame_happy_path(test_client, git_repo):
    """GET /api/v1/repos/{alias}/git/blame returns 200 with correct lines list.

    Derives expected line count and content from the actual hello.txt in the
    fixture repo so the test stays in sync with conftest changes.
    """
    repo, _ = git_repo
    # Derive ground truth from the actual committed file
    committed_lines = (repo / "hello.txt").read_text().splitlines()
    expected_count = len(committed_lines)

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/blame?path=hello.txt")

    assert response.status_code == 200, response.text
    data = response.json()
    assert "lines" in data
    lines = data["lines"]
    assert isinstance(lines, list)
    assert len(lines) == expected_count, (
        f"Expected {expected_count} blame lines but got {len(lines)}"
    )

    # Validate full ordered line_number sequence [1 .. n]
    assert [e["line_number"] for e in lines] == list(range(1, expected_count + 1)), (
        f"line_number sequence incorrect: {[e['line_number'] for e in lines]}"
    )

    for i, entry in enumerate(lines):
        assert set(entry.keys()) == _LINE_KEYS, (
            f"Line entry keys {set(entry.keys())} differ from expected {_LINE_KEYS}"
        )
        assert isinstance(entry["line_number"], int)
        assert isinstance(entry["commit_hash"], str)
        assert len(entry["commit_hash"]) == 40
        assert isinstance(entry["author"], str)
        assert isinstance(entry["date"], str)
        assert entry["content"] == committed_lines[i]


def test_git_blame_path_traversal_returns_400(test_client, git_repo):
    """GET git/blame with path containing '..' returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/blame?path=../etc/passwd")

    assert response.status_code == 400


def test_git_blame_absolute_path_returns_400(test_client, git_repo):
    """GET git/blame with absolute path returns 400."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/blame?path=/etc/passwd")

    assert response.status_code == 400


def test_git_blame_unknown_alias_returns_404(test_client):
    """GET git/blame returns 404 when alias is not activated."""
    with _arm_not_found():
        response = test_client.get(
            "/api/v1/repos/no_such_repo/git/blame?path=hello.txt"
        )

    assert response.status_code == 404


def test_git_blame_file_not_found_returns_404(test_client, git_repo):
    """GET git/blame for non-existent file returns 404."""
    repo, _ = git_repo

    with _arm_repo(repo):
        response = test_client.get(
            "/api/v1/repos/myrepo/git/blame?path=nonexistent.txt"
        )

    assert response.status_code == 404


def test_git_blame_no_auth_returns_401_or_403():
    """GET /api/v1/repos/{alias}/git/blame without auth returns 401 or 403."""
    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/repos/somerepo/git/blame?path=hello.txt")
        assert response.status_code in (401, 403)
    finally:
        app.dependency_overrides.update(previous_overrides)
