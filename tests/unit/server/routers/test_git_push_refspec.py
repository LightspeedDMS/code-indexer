"""REST `POST .../git/push` must accept a `src:dst` refspec in `branch`
(e.g. `feature:main`), not just a bare branch name. The refspec is one
argv element, so only a leading '-' on the whole element is rejected; a
side such as `-` in `main:-` is a ref name that git itself handles.

NOTE: asserts the git-level effect (the remote's `main` ref advances to
`feature`'s tip), not a clean HTTP 200 -- `GitPushResponse` requires
`branch`/`remote`/`commits_pushed` fields that the plain `git_push()`
result dict does not populate, a pre-existing, unrelated field-shape
mismatch that surfaces as an HTTP 500 on any real push success (not
specific to this test's inputs).

Exercised through the real REST route against a REAL throwaway git
repository with a local bare remote (no mocking of git itself, per this
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

_BASE = "/api/v1/repos/myrepo/git"


def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo_with_golden_remote(tmp_path: Path):
    remote = tmp_path / "golden.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    _git(["checkout", "-q", "-b", "main"], repo)
    (repo / "f.txt").write_text("hello\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    _git(["remote", "add", "golden", str(remote)], repo)
    _git(["push", "-q", "--set-upstream", "golden", "main"], repo)

    _git(["checkout", "-q", "-b", "feature"], repo)
    (repo / "f.txt").write_text("hello\nfeature change\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-q", "-m", "feature commit"], repo)
    _git(["checkout", "-q", "main"], repo)

    return repo, remote


def _rev_parse(repo_path: Path, ref: str) -> str:
    return str(_git(["rev-parse", ref], repo_path).stdout.strip())


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


class TestGitPushSrcDstRefspec:
    def test_src_colon_dst_refspec_pushes_feature_onto_main(
        self, test_client, tmp_path
    ):
        repo, remote = _make_repo_with_golden_remote(tmp_path)
        feature_head = _rev_parse(repo, "feature")

        with _arm_repo(repo):
            response = test_client.post(
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "feature:main"},
            )

        assert "must not start with" not in response.text, (
            f"POST push branch='feature:main' (a src:dst refspec) must not "
            f"be rejected by validate_branch_name, got "
            f"{response.status_code}: {response.text}"
        )
        assert _rev_parse(remote, "main") == feature_head, (
            "branch='feature:main' must push feature's tip onto the remote's main ref"
        )

    def test_dash_named_dst_side_reaches_git(self, test_client, tmp_path):
        """A refspec side is a ref name, never an option: the whole argv
        element `main:-` does not start with '-', so git itself pushes
        main onto a remote branch literally named `-`."""
        repo, remote = _make_repo_with_golden_remote(tmp_path)

        with _arm_repo(repo):
            response = test_client.post(
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "main:-"},
            )

        assert "must not start with" not in response.text, response.text
        assert _rev_parse(remote, "refs/heads/-") == _rev_parse(repo, "main")

    def test_commit_ish_src_refspec_creates_named_remote_ref(
        self, test_client, tmp_path
    ):
        """A refspec source may be any commit-ish expression, not just a
        plain ref name -- `check-ref-format` would reject `feature~1`."""
        repo, remote = _make_repo_with_golden_remote(tmp_path)
        feature_tilde_1 = _rev_parse(repo, "feature~1")

        with _arm_repo(repo):
            response = test_client.post(
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "feature~1:refs/heads/older"},
            )

        assert "must not start with" not in response.text, (
            f"branch='feature~1:refs/heads/older' must not be rejected, got "
            f"{response.status_code}: {response.text}"
        )
        assert _rev_parse(remote, "refs/heads/older") == feature_tilde_1

    def test_glob_refspec_pushes_matching_branches(self, test_client, tmp_path):
        """A refspec side may contain a glob -- `check-ref-format` would
        reject `refs/heads/*`."""
        repo, remote = _make_repo_with_golden_remote(tmp_path)
        feature_head = _rev_parse(repo, "feature")

        with _arm_repo(repo):
            response = test_client.post(
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "refs/heads/*:refs/heads/*"},
            )

        assert "must not start with" not in response.text, (
            f"branch='refs/heads/*:refs/heads/*' must not be rejected, got "
            f"{response.status_code}: {response.text}"
        )
        assert _rev_parse(remote, "refs/heads/feature") == feature_head
