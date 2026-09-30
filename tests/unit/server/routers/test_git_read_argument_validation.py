"""REST git_cat / git_blame / git_file_history must validate their `rev`
query parameter against real git argv before any subprocess: a revision
value must never start with '-', so a caller-controlled option (such as
git blame's `--contents=<path>` or git log's `--output=<path>`) is always
treated as a rejected value, never as a real git option.

All tests exercise the real REST routes through FastAPI's TestClient
against a REAL throwaway git repository (no mocking of git itself, per
this repo's Anti-Mock rule). Only the alias-to-filesystem-path resolution
layer (`routers.git._get_activated_repo_manager`) is patched, matching the
pattern already used by test_git_blame_endpoint.py /
test_git_cat_endpoint.py.
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
    (repo / "hello.txt").write_text("line one\nline two\n")
    _git(["add", "hello.txt"], repo)
    _git(["commit", "-q", "-m", "first commit"], repo)
    (repo / "hello.txt").write_text("line one\nline two\nline three\n")
    _git(["add", "hello.txt"], repo)
    _git(["commit", "-q", "-m", "second commit"], repo)
    return repo


@contextmanager
def _arm_repo(repo_path: Path):
    """Patch git router's _get_activated_repo_manager() to resolve
    repo_path (mirrors test_git_blame_endpoint.py's helper)."""
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
def test_client(mock_user):
    def override():
        return mock_user

    app.dependency_overrides[get_current_user] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


class TestGitCatRevisionValidation:
    def test_rejects_option_shaped_rev(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/cat",
                params={"path": "hello.txt", "rev": "--abbrev-commit"},
            )
        assert response.status_code == 400, response.text
        assert "must not start with '-'" in response.text

    def test_accepts_head(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/cat",
                params={"path": "hello.txt", "rev": "HEAD"},
            )
        assert response.status_code == 200, response.text
        assert "line three" in response.json()["content"]

    def test_accepts_head_tilde_1(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/cat",
                params={"path": "hello.txt", "rev": "HEAD~1"},
            )
        assert response.status_code == 200, response.text
        assert "line three" not in response.json()["content"]

    def test_accepts_full_sha(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/cat",
                params={"path": "hello.txt", "rev": sha},
            )
        assert response.status_code == 200, response.text


class TestGitBlameRevisionValidation:
    def test_rejects_option_shaped_revision(self, test_client, tmp_path):
        outside_file = tmp_path / "outside_marker.txt"
        outside_file.write_text("marker-content\n")
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/blame",
                params={"path": "hello.txt", "rev": f"--contents={outside_file}"},
            )
        assert response.status_code == 400, response.text
        assert "marker-content" not in response.text, (
            "rev must resolve via git rev-parse --verify and never start with '-'"
        )

    def test_accepts_head(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/blame",
                params={"path": "hello.txt", "rev": "HEAD"},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["lines"]) == 3

    def test_accepts_branch_with_slash(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        _git(["checkout", "-q", "-b", "feature/x"], repo)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/blame",
                params={"path": "hello.txt", "rev": "feature/x"},
            )
        assert response.status_code == 200, response.text


class TestGitFileHistoryRevisionValidation:
    def test_rejects_output_option_never_writes_file(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        marker = tmp_path / "file_history_output_marker.txt"
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/file-history",
                params={"path": "hello.txt", "rev": f"--output={marker}"},
            )
        assert response.status_code == 400, response.text
        assert not marker.exists(), (
            "rev must resolve via git rev-parse --verify and never start with '-'"
        )

    def test_accepts_head(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/file-history",
                params={"path": "hello.txt", "rev": "HEAD"},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["commits"]) == 2

    def test_accepts_no_rev(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.get(
                "/api/v1/repos/myrepo/git/file-history",
                params={"path": "hello.txt"},
            )
        assert response.status_code == 200, response.text
        assert len(response.json()["commits"]) == 2
