"""
Unit tests for GET /api/repos/{alias}/files endpoint.

Bug #824 — missing repos files handler.

Tests:
  test_repos_files_happy_path                  -- 200 with file listing
  test_repos_files_unknown_alias_returns_404   -- 404 when alias not activated
  test_repos_files_path_traversal_returns_400  -- 400 on path traversal (.. or absolute)
  test_repos_files_no_auth_returns_401_or_403  -- 401/403 without auth (separate add)
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture(scope="module")
def test_client(mock_user):
    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


@contextmanager
def arm_mock(metadata, repo_path: str):
    """Patch activated_repo_manager closure in the files handler."""
    handler = _find_route_handler("/api/repos/{user_alias}/files", "GET")
    mock_arm = Mock()
    mock_arm._load_metadata.return_value = metadata
    mock_arm.get_activated_repo_path.return_value = repo_path
    with _patch_closure(handler, "activated_repo_manager", mock_arm):
        yield mock_arm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_repos_files_happy_path(test_client, tmp_path):
    """GET /api/repos/{alias}/files returns 200 with non-empty files list."""
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("hello")
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("x = 1")

    with arm_mock({"user_alias": "myrepo"}, str(repo_dir)):
        response = test_client.get("/api/repos/myrepo/files")

    assert response.status_code == 200
    data = response.json()
    assert "files" in data
    assert isinstance(data["files"], list)
    assert len(data["files"]) >= 1
    names = [f["name"] for f in data["files"]]
    assert "README.md" in names


def test_repos_files_unknown_alias_returns_404(test_client, tmp_path):
    """GET /api/repos/{alias}/files returns 404 when alias is not activated."""
    non_existent = tmp_path / "no_such_repo"

    with arm_mock(None, str(non_existent)):
        response = test_client.get("/api/repos/no_such_repo/files")

    assert response.status_code == 404


def test_repos_files_path_traversal_returns_400(test_client, tmp_path):
    """GET /api/repos/{alias}/files?path=.. or /abs returns 400."""
    repo_dir = tmp_path / "myrepo2"
    repo_dir.mkdir()

    with arm_mock({"user_alias": "myrepo2"}, str(repo_dir)):
        r1 = test_client.get("/api/repos/myrepo2/files?path=../../etc")
        assert r1.status_code == 400

        r2 = test_client.get("/api/repos/myrepo2/files?path=/etc/passwd")
        assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Recursive listing (one call instead of N per-directory round-trips)
# ---------------------------------------------------------------------------


def _nested_repo(tmp_path):
    repo_dir = tmp_path / "deep"
    (repo_dir / "a" / "b" / "c").mkdir(parents=True)
    (repo_dir / "top.txt").write_text("t")
    (repo_dir / "a" / "one.py").write_text("1")
    (repo_dir / "a" / "b" / "two.py").write_text("2")
    (repo_dir / "a" / "b" / "c" / "three.py").write_text("3")
    return repo_dir


def test_repos_files_recursive_returns_full_tree(test_client, tmp_path):
    """recursive=true returns the whole subtree in one response."""
    repo_dir = _nested_repo(tmp_path)
    with arm_mock({"user_alias": "deep"}, str(repo_dir)):
        response = test_client.get("/api/repos/deep/files?recursive=true")

    assert response.status_code == 200
    data = response.json()
    paths = {f["path"] for f in data["files"]}
    # deep files reachable without per-directory calls
    assert "a/b/c/three.py" in paths
    assert "a/one.py" in paths
    assert data["truncated"] is False
    assert data["count"] == len(data["files"])


def test_repos_files_recursive_max_depth_prunes(test_client, tmp_path):
    """max_depth bounds how far recursion descends."""
    repo_dir = _nested_repo(tmp_path)
    with arm_mock({"user_alias": "deep"}, str(repo_dir)):
        response = test_client.get("/api/repos/deep/files?recursive=true&max_depth=1")

    assert response.status_code == 200
    paths = {f["path"] for f in response.json()["files"]}
    assert "top.txt" in paths
    assert "a" in paths  # the dir entry at depth 1
    assert "a/one.py" not in paths  # depth 2 not descended
    assert "a/b/c/three.py" not in paths


def test_repos_files_recursive_limit_truncates(test_client, tmp_path):
    """limit caps entries and flags truncated."""
    repo_dir = _nested_repo(tmp_path)
    with arm_mock({"user_alias": "deep"}, str(repo_dir)):
        response = test_client.get("/api/repos/deep/files?recursive=true&limit=2")

    assert response.status_code == 200
    data = response.json()
    assert data["truncated"] is True
    assert len(data["files"]) == 2
