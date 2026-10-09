"""REST git push/pull/fetch (and every other mutation route in
routers/git.py) must enforce the SAME permission as their MCP twins
(mcp/tool_docs/git/*.md `required_permission`), so a bare `normal_user`
gets 403, not 200, exactly like the MCP tool already does.

Permission-tier mapping applied here (looked up from each MCP twin's
`required_permission`, not guessed):
  - repository:read / query_repos  -> status, diff, log, branches (list),
                                       cat, blame, file-history
  - repository:write               -> stage, unstage, commit, push, pull,
                                       fetch, merge-abort, checkout-file,
                                       branches (create), branches/switch
  - repository:admin                -> reset, clean, branches (delete)

Each route is exercised with a repository alias that does not exist, with
the repo-path resolution mocked to raise FileNotFoundError so that any
request which gets PAST the permission check reaches the service/manager
layer and fails with 404 -- never 403. A request that is blocked by the
permission check must get 403 and must NEVER reach that resolution layer
at all (asserted via mock.assert_not_called()).

This directly discriminates "permission check exists" from "permission
check is missing": on an unfixed router, EVERY route would respond to a
normal_user without a 403 (the resolution mock's FileNotFoundError
surfaces as 404 for read/write/admin routes alike).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.git_operations_service import git_operations_service


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _user(role: UserRole) -> User:
    return User(
        username=f"test_{role.value}",
        role=role,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


NORMAL_USER = _user(UserRole.NORMAL_USER)
POWER_USER = _user(UserRole.POWER_USER)
ADMIN_USER = _user(UserRole.ADMIN)


@pytest.fixture
def client(tmp_path):
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _access_service_admin,
    )

    c = TestClient(app)
    # Every caller is an admin of a real access service, so the
    # activated-repo guard passes; these tests pin the permission tiers.
    with _access_service_admin(
        tmp_path / "access-groups.db",
        NORMAL_USER.username,
        POWER_USER.username,
        ADMIN_USER.username,
    ):
        yield c
    app.dependency_overrides.pop(get_current_user, None)


@contextmanager
def _repo_not_found():
    """Make every repo-path resolution route in git.py raise
    FileNotFoundError, on BOTH resolution paths the router uses:
      - git_operations_service.activated_repo_manager (status/diff/log/
        stage/unstage/commit/push/pull/fetch/reset/clean/merge-abort/
        checkout-file/branches)
      - app.state.activated_repo_manager (cat/blame/file-history, via
        routers/git.py's _get_activated_repo_manager())
    """
    mock_arm = MagicMock()
    mock_arm.get_activated_repo_path.side_effect = FileNotFoundError(
        "repository not found"
    )
    original_svc_arm = git_operations_service.activated_repo_manager
    git_operations_service.activated_repo_manager = mock_arm

    from code_indexer.server import app as app_module

    _UNSET = object()
    saved_state_arm = getattr(app_module.app.state, "activated_repo_manager", _UNSET)
    app_module.app.state.activated_repo_manager = mock_arm

    try:
        yield mock_arm
    finally:
        git_operations_service.activated_repo_manager = original_svc_arm
        if saved_state_arm is _UNSET:
            if hasattr(app_module.app.state, "activated_repo_manager"):
                delattr(app_module.app.state, "activated_repo_manager")
        else:
            app_module.app.state.activated_repo_manager = saved_state_arm


def _call_as(client: TestClient, user: User, method: str, path: str, **kwargs):
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        return client.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


_ALIAS = "nonexistent-repo-git-permissions"
_BASE = f"/api/v1/repos/{_ALIAS}/git"

# (method, path, kwargs, required_tier)
# required_tier in {"read", "write", "admin"} maps to the permission each
# route's MCP twin declares (see module docstring table).
ROUTES: list[tuple[str, str, Dict[str, Any], str]] = [
    ("GET", f"{_BASE}/status", {}, "read"),
    ("GET", f"{_BASE}/diff", {}, "read"),
    ("GET", f"{_BASE}/log", {}, "read"),
    ("POST", f"{_BASE}/stage", {"json": {"file_paths": ["a.txt"]}}, "write"),
    ("POST", f"{_BASE}/unstage", {"json": {"file_paths": ["a.txt"]}}, "write"),
    ("POST", f"{_BASE}/commit", {"json": {"message": "m"}}, "write"),
    ("POST", f"{_BASE}/push", {"json": {}}, "write"),
    ("POST", f"{_BASE}/pull", {"json": {}}, "write"),
    ("POST", f"{_BASE}/fetch", {"json": {}}, "write"),
    ("POST", f"{_BASE}/reset", {"json": {"mode": "soft"}}, "admin"),
    ("POST", f"{_BASE}/clean", {"json": {}}, "admin"),
    ("POST", f"{_BASE}/merge-abort", {}, "write"),
    ("POST", f"{_BASE}/checkout-file", {"json": {"file_path": "a.txt"}}, "write"),
    ("GET", f"{_BASE}/branches", {}, "read"),
    ("POST", f"{_BASE}/branches", {"json": {"branch_name": "x"}}, "write"),
    ("POST", f"{_BASE}/branches/x/switch", {}, "write"),
    ("DELETE", f"{_BASE}/branches/x", {}, "admin"),
    ("GET", f"{_BASE}/cat", {"params": {"path": "a.txt"}}, "read"),
    ("GET", f"{_BASE}/blame", {"params": {"path": "a.txt"}}, "read"),
    ("GET", f"{_BASE}/file-history", {"params": {"path": "a.txt"}}, "read"),
]

_ROUTE_IDS = [f"{m}:{p.split('/git/', 1)[1]}" for m, p, _, _ in ROUTES]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNormalUserBlockedFromWriteAndAdminRoutes:
    """normal_user has query_repos + repository:read only -- must get 403
    on every 'write' and 'admin' tier route, and must NEVER reach the
    repo-resolution layer for those (mock stays uncalled)."""

    @pytest.mark.parametrize("method,path,kwargs,tier", ROUTES, ids=_ROUTE_IDS)
    def test_normal_user_write_or_admin_route_is_403(
        self, client, method, path, kwargs, tier
    ):
        if tier == "read":
            pytest.skip("read-tier route: normal_user is authorized, see other test")

        with _repo_not_found() as mock_arm:
            response = _call_as(client, NORMAL_USER, method, path, **kwargs)

        assert response.status_code == 403, (
            f"{method} {path} (tier={tier}): normal_user must get 403, got "
            f"{response.status_code}: {response.text}"
        )
        mock_arm.get_activated_repo_path.assert_not_called()


class TestNormalUserAllowedOnReadRoutes:
    """normal_user must NOT be blocked on read-tier routes (query_repos /
    repository:read are base NORMAL_USER permissions)."""

    @pytest.mark.parametrize("method,path,kwargs,tier", ROUTES, ids=_ROUTE_IDS)
    def test_normal_user_read_route_is_not_403(
        self, client, method, path, kwargs, tier
    ):
        if tier != "read":
            pytest.skip("non-read-tier route covered by the write/admin test")

        with _repo_not_found():
            response = _call_as(client, NORMAL_USER, method, path, **kwargs)

        assert response.status_code != 403, (
            f"{method} {path} (tier={tier}): normal_user must be authorized "
            f"(query_repos/repository:read are base permissions), got 403"
        )


class TestPowerUserAllowedOnWriteRoutesBlockedOnAdmin:
    """power_user has repository:write (+ inherited read tier) but NOT
    repository:admin."""

    @pytest.mark.parametrize("method,path,kwargs,tier", ROUTES, ids=_ROUTE_IDS)
    def test_power_user_admin_route_is_403(self, client, method, path, kwargs, tier):
        if tier != "admin":
            pytest.skip("non-admin-tier route covered by other tests")

        with _repo_not_found() as mock_arm:
            response = _call_as(client, POWER_USER, method, path, **kwargs)

        assert response.status_code == 403, (
            f"{method} {path} (tier={tier}): power_user lacks "
            f"repository:admin and must get 403, got {response.status_code}"
        )
        mock_arm.get_activated_repo_path.assert_not_called()

    @pytest.mark.parametrize("method,path,kwargs,tier", ROUTES, ids=_ROUTE_IDS)
    def test_power_user_write_or_read_route_is_not_403(
        self, client, method, path, kwargs, tier
    ):
        if tier == "admin":
            pytest.skip("admin-tier route covered by the admin test above")

        with _repo_not_found():
            response = _call_as(client, POWER_USER, method, path, **kwargs)

        assert response.status_code != 403, (
            f"{method} {path} (tier={tier}): power_user must be authorized, got 403"
        )


class TestAdminUserAllowedOnEveryRoute:
    """admin inherits repository:admin + repository:write + read tier --
    must never be blocked on any route in this router."""

    @pytest.mark.parametrize("method,path,kwargs,tier", ROUTES, ids=_ROUTE_IDS)
    def test_admin_user_never_gets_403(self, client, method, path, kwargs, tier):
        with _repo_not_found():
            response = _call_as(client, ADMIN_USER, method, path, **kwargs)

        assert response.status_code != 403, (
            f"{method} {path} (tier={tier}): admin must be authorized, got 403"
        )


# ---------------------------------------------------------------------------
# Push/pull/fetch specific permission checks
# ---------------------------------------------------------------------------


class TestPushPullFetchPermissionSpecific:
    """normal_user must get 403 (not 200) on push/pull/fetch, and
    power_user/admin must reach the service layer (404, repo not found)."""

    @pytest.mark.parametrize(
        "path,body",
        [
            (f"{_BASE}/push", {}),
            (f"{_BASE}/pull", {}),
            (f"{_BASE}/fetch", {}),
        ],
    )
    def test_normal_user_403_power_user_reaches_service(
        self, client, path: str, body: Optional[dict]
    ):
        with _repo_not_found():
            normal_response = _call_as(client, NORMAL_USER, "POST", path, json=body)
        assert normal_response.status_code == 403, (
            f"POST {path}: normal_user must get 403, "
            f"got {normal_response.status_code}: {normal_response.text}"
        )

        with _repo_not_found() as mock_arm:
            power_response = _call_as(client, POWER_USER, "POST", path, json=body)
        assert power_response.status_code == 404, (
            f"POST {path}: power_user must reach the service layer (404, "
            f"repo not found), got {power_response.status_code}: "
            f"{power_response.text}"
        )
        mock_arm.get_activated_repo_path.assert_called()


# ---------------------------------------------------------------------------
# `branch="+main"` (git's own force-push refspec syntax) must reach `git
# push` unchanged over the REST front door: `git check-ref-format
# --allow-onelevel '+main'` is accepted by real git, and `git push
# --end-of-options <remote> +main` force-pushes anyway (verified
# empirically), so `validate_branch_name` keeps the leading '+' rather
# than rejecting it (only a leading '-' on the whole element is a hazard).
# ---------------------------------------------------------------------------


def _git(args: list, cwd) -> None:
    import subprocess

    subprocess.run(
        ["git"] + args, cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _make_repo_with_golden_remote(tmp_path):
    """Real repo with a commit, on branch 'main', with a local-path 'golden'
    remote -- matching how every activated repo is configured."""
    remote = tmp_path / "golden.git"
    remote.mkdir()
    _git(["init", "-q", "--bare"], cwd=remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test User"], cwd=repo)
    _git(["checkout", "-q", "-b", "main"], cwd=repo)
    (repo / "f.txt").write_text("hello\n")
    _git(["add", "f.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    _git(["remote", "add", "golden", str(remote)], cwd=repo)
    _git(["push", "-q", "--set-upstream", "golden", "main"], cwd=repo)

    return repo, remote


@contextmanager
def _repo_resolves_to(repo_path):
    """Make repo-path resolution return a REAL repo path instead of raising,
    so a request that clears the permission check reaches the real
    GitOperationsService.git_push -> validate_branch_name path (real git,
    no mocking of git itself)."""
    mock_arm = MagicMock()
    mock_arm.get_activated_repo_path.return_value = str(repo_path)
    # prepare_remote_operation -> Optional[str]: the registered repository
    # URL whose credentials are supplied at run time; None when no golden
    # repository is registered (the local "golden" remote needs none).
    mock_arm.prepare_remote_operation.return_value = None
    original_svc_arm = git_operations_service.activated_repo_manager
    git_operations_service.activated_repo_manager = mock_arm
    try:
        yield mock_arm
    finally:
        git_operations_service.activated_repo_manager = original_svc_arm


class TestPlusMainForcePushRest:
    def test_plus_main_force_pushes_and_advances_remote(self, client, tmp_path):
        """`branch="+main"` is git's own force-push refspec syntax --
        accepted by `git check-ref-format --allow-onelevel` and honored
        by `git push <remote> +main` even behind `--end-of-options`
        (verified empirically: the push still reports "(forced
        update)"). It is the only force-push mechanism available through
        this route (no separate `force` request field), so it must reach
        `git push` unchanged and rewrite a diverged remote ref. Asserts
        both the HTTP 200 response and the git-level effect.
        """
        repo, remote = _make_repo_with_golden_remote(tmp_path)
        # Diverge local history from what is already on the remote (the
        # way an amend does), so only a force-push can advance the
        # remote's main ref.
        _git(["commit", "--amend", "-q", "-m", "init (amended)"], cwd=repo)
        local_head = _rev_parse(repo, "HEAD")
        head_before = _rev_parse(remote, "main")
        assert local_head != head_before

        with _repo_resolves_to(repo):
            response = _call_as(
                client,
                POWER_USER,
                "POST",
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "+main"},
            )

        assert response.status_code == 200, response.text
        assert "must not start with" not in response.text, (
            f"POST push branch='+main' (git's own force-push syntax) must "
            f"not be rejected by validate_branch_name, got "
            f"{response.status_code}: {response.text}"
        )
        assert _rev_parse(remote, "main") == local_head, (
            "branch='+main' must force-push and advance the remote's diverged main ref"
        )

    def test_normal_branch_main_push_still_succeeds(self, client, tmp_path):
        """branch='main' must not be rejected by validate_branch_name's
        leading-'+' check, and the push must actually reach the remote.
        Asserts both the HTTP 200 response and the git-level effect.
        """
        repo, remote = _make_repo_with_golden_remote(tmp_path)
        (repo / "f.txt").write_text("second commit\n")
        _git(["add", "f.txt"], cwd=repo)
        _git(["commit", "-q", "-m", "second"], cwd=repo)
        head_before = _rev_parse(remote, "main")
        local_head = _rev_parse(repo, "HEAD")

        with _repo_resolves_to(repo):
            response = _call_as(
                client,
                POWER_USER,
                "POST",
                f"{_BASE}/push",
                json={"remote": "golden", "branch": "main"},
            )

        assert response.status_code == 200, response.text
        assert "must not start with" not in response.text, (
            f"POST push branch='main' (legitimate) must not be rejected by "
            f"validate_branch_name, got {response.status_code}: "
            f"{response.text}"
        )
        assert _rev_parse(remote, "main") == local_head != head_before, (
            "POST push branch='main' (legitimate) must actually push the "
            "new commit to the remote"
        )


def _rev_parse(repo_path, ref: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
