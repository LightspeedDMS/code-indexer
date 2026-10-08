"""REST `POST .../branches/{name}/switch` and `DELETE .../branches/{name}`
must map a `GitArgumentValidationError` specifically (not the broader
`ValueError`) to HTTP 400 -- an argv-safety rejection (e.g. a leading
'-') gets the clean 400 `POST .../branches` (create) already has, while an
invalid confirmation token is not an argv-safety rejection: it answers 200
with `requires_confirmation` and a fresh token.

Also covers `switch` accepting the exact value `-` (git's own
`checkout -` shorthand for the previously checked out branch).

Exercised through the real REST routes against a REAL throwaway git
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
from code_indexer.server.services.git_operations_service import (
    git_operations_service,
)

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
    _git(["commit", "-q", "-m", "c1"], repo)
    return repo


@contextmanager
def _arm_repo(repo_path: Path):
    mock_arm = Mock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    # Swap the backing slot, never read the property: reading it constructs
    # a real ActivatedRepoManager that would then stay on the singleton.
    with patch.object(git_operations_service, "_activated_repo_manager_lazy", mock_arm):
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


class TestGitBranchSwitchValidationStatus:
    def test_leading_dash_returns_400_not_500(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.post(f"{_BASE}/branches/-x/switch")
        assert response.status_code == 400, response.text
        assert "must not start with '-'" in response.text

    def test_accepts_bare_dash_previous_branch_shorthand(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        _git(["checkout", "-q", "-b", "feature"], repo)
        _git(["checkout", "-q", "main"], repo)
        # "-" now refers to "feature", the branch just switched away from.
        with _arm_repo(repo):
            response = test_client.post(f"{_BASE}/branches/-/switch")
        assert response.status_code == 200, response.text
        # The response echoes the caller's literal input ("-") rather than
        # the resolved branch name, so the real git-level effect is
        # asserted instead.
        current = _git(["branch", "--show-current"], repo).stdout.strip()
        assert current == "feature"


class TestGitBranchDeleteValidationStatus:
    def test_leading_dash_returns_400_not_500(self, test_client, tmp_path):
        repo = _make_repo(tmp_path)
        with _arm_repo(repo):
            response = test_client.delete(f"{_BASE}/branches/-x")
        assert response.status_code == 400, response.text
        assert "must not start with '-'" in response.text

    def test_invalid_confirmation_token_returns_fresh_token(
        self, test_client, tmp_path
    ):
        """An invalid token is not an argv-safety rejection: the route
        answers 200 with `requires_confirmation` and a fresh token, and the
        branch is not deleted."""
        repo = _make_repo(tmp_path)
        _git(["checkout", "-q", "-b", "feature"], repo)
        _git(["checkout", "-q", "main"], repo)
        with _arm_repo(repo):
            response = test_client.delete(
                f"{_BASE}/branches/feature",
                params={"confirmation_token": "bogus"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["requires_confirmation"] is True
        assert isinstance(body["token"], str) and body["token"] != "bogus"
        assert "feature" in _git(["branch", "--list", "feature"], repo).stdout
