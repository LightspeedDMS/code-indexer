"""
Unit tests for GET git read endpoints response contract.

Bug #825 — cli_git.py used wrong response key names when displaying output:
  - git_log displayed c.get("hash") instead of c.get("commit_hash")
  - git_diff displayed result.get("diff") instead of result.get("diff_text")
  - git_branches displayed result.get("branches") instead of result.get("local")

These tests lock in the server contract so the cli_git.py key-name fixes can
be validated against a known-good truth.

Tests (4 required by spec):
  test_git_log_response_shape_matches_GitLogResponse
  test_git_log_tolerates_commit_message_with_quotes_and_backslashes
  test_git_branches_returns_list_without_typeerror
  test_git_diff_returns_text_without_typeerror
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.services.git_operations_service import git_operations_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _mock_user():
    user = Mock()
    user.username = "testuser"
    return user


@pytest.fixture(scope="module")
def test_client(_mock_user):
    def _override():
        return _mock_user

    app.dependency_overrides[get_current_user] = _override
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path, commit_message: str = "initial commit") -> None:
    """Initialise a real git repo at *repo* with one commit."""
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("hello")
    subprocess.run(
        ["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


@contextmanager
def _arm_repo(repo_path: Path):
    """Patch ActivatedRepoManager on the singleton service to return *repo_path*."""
    mock_arm = MagicMock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    original = git_operations_service.activated_repo_manager
    git_operations_service.activated_repo_manager = mock_arm
    try:
        yield mock_arm
    finally:
        git_operations_service.activated_repo_manager = original


# ---------------------------------------------------------------------------
# Test 1: git log response shape uses commit_hash (not hash)
# ---------------------------------------------------------------------------


def test_git_log_response_shape_matches_GitLogResponse(test_client, tmp_path):
    """Server GET .../git/log returns commits with 'commit_hash' key.

    Bug #825: cli_git.py consumed the response using c.get('hash', '') but
    the server sends GitCommitInfo which has 'commit_hash'.  This test locks
    in the server contract.
    """
    repo = tmp_path / "log_repo"
    repo.mkdir()
    _init_git_repo(repo)

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/log")

    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}: {response.text}"
    )
    data = response.json()

    assert "commits" in data, f"Response missing 'commits': {data}"
    assert isinstance(data["commits"], list)
    assert len(data["commits"]) >= 1, "Expected at least one commit in log"

    commit = data["commits"][0]
    # Must be 'commit_hash', NOT 'hash' — cli_git used the wrong key (Bug #825)
    assert "commit_hash" in commit, (
        f"Commit missing 'commit_hash' key (cli_git used wrong key 'hash'): {commit}"
    )
    assert "hash" not in commit, (
        f"Commit unexpectedly has 'hash' key — server contract changed: {commit}"
    )
    assert "author" in commit
    assert "date" in commit
    assert "message" in commit

    # Full SHA-40 hash
    assert len(commit["commit_hash"]) == 40, (
        f"commit_hash should be a full 40-char SHA, got: {commit['commit_hash']!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: commit message with quotes and backslashes — no 500
# ---------------------------------------------------------------------------


def test_git_log_tolerates_commit_message_with_quotes_and_backslashes(
    test_client, tmp_path
):
    """GET .../git/log returns 200 even with special chars in the commit message.

    Commit messages may contain double-quotes, single-quotes, and backslashes
    (Windows paths, shell escape sequences).  The server must serialise these
    as valid JSON without raising a 500.
    """
    special_message = r'fix: handle path C:\Users\foo and say "hello" it\'s fine'
    repo = tmp_path / "special_repo"
    repo.mkdir()
    _init_git_repo(repo, commit_message=special_message)

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/log")

    assert response.status_code == 200, (
        f"Expected 200 for special-char commit message but got "
        f"{response.status_code}: {response.text}"
    )
    data = response.json()
    assert "commits" in data
    assert len(data["commits"]) >= 1

    first_message = data["commits"][0]["message"]
    assert isinstance(first_message, str)
    assert len(first_message) > 0, "Commit message must survive JSON roundtrip non-empty"


# ---------------------------------------------------------------------------
# Test 3: branches response shape uses local/remote/current (not branches)
# ---------------------------------------------------------------------------


def test_git_branches_returns_list_without_typeerror(test_client, tmp_path):
    """GET .../git/branches returns 200 with 'local', 'remote', 'current' keys.

    Bug #825: cli_git.py used result.get('branches', []) but the server sends
    GitBranchListResponse with keys 'local', 'remote', 'current'.
    """
    repo = tmp_path / "branches_repo"
    repo.mkdir()
    _init_git_repo(repo)

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/branches")

    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}: {response.text}"
    )
    data = response.json()

    # Must be 'local' and 'remote', NOT 'branches' — cli_git Bug #825
    assert "local" in data, (
        f"Response missing 'local' key (cli_git used wrong key 'branches'): {data}"
    )
    assert "remote" in data, f"Response missing 'remote' key: {data}"
    assert "current" in data, f"Response missing 'current' key: {data}"
    assert "branches" not in data, (
        f"Response unexpectedly has 'branches' key — server contract changed: {data}"
    )

    assert isinstance(data["local"], list)
    assert isinstance(data["remote"], list)
    assert isinstance(data["current"], str)

    assert len(data["local"]) >= 1, (
        f"Expected at least one local branch, got: {data['local']}"
    )


# ---------------------------------------------------------------------------
# Test 4: diff response shape uses diff_text (not diff)
# ---------------------------------------------------------------------------


def test_git_diff_returns_text_without_typeerror(test_client, tmp_path):
    """GET .../git/diff returns 200 with 'diff_text' key.

    Bug #825: cli_git.py used result.get('diff', '') but the server sends
    GitDiffResponse with key 'diff_text'.
    """
    repo = tmp_path / "diff_repo"
    repo.mkdir()
    _init_git_repo(repo)
    # Add an uncommitted file so the diff response can include something
    (repo / "newfile.txt").write_text("new content for diff")

    with _arm_repo(repo):
        response = test_client.get("/api/v1/repos/myrepo/git/diff")

    assert response.status_code == 200, (
        f"Expected 200 but got {response.status_code}: {response.text}"
    )
    data = response.json()

    # Must be 'diff_text', NOT 'diff' — cli_git Bug #825
    assert "diff_text" in data, (
        f"Response missing 'diff_text' key (cli_git used wrong key 'diff'): {data}"
    )
    assert "diff" not in data, (
        f"Response unexpectedly has 'diff' key — server contract changed: {data}"
    )

    assert isinstance(data["diff_text"], str)
    assert "files_changed" in data
    assert isinstance(data["files_changed"], int)
