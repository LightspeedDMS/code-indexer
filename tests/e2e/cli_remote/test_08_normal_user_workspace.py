"""Phase 4 E2E tests: normal-user workspace operations (Story #981 AC1, AC2).

Verifies that NORMAL_USER role users can perform the workspace-management
actions freed up by Story #981:
  - list available golden repos (AC1)
  - activate / list / sync / deactivate their own workspace (AC1)

And the security guard for AC2:
  - NORMAL_USER cannot switch branches on *-global aliases — 403 (admin-only).

Each test uses a fresh NORMAL_USER and an isolated workspace so the admin
session-scoped fixtures (used by tests 01-07) are not perturbed.

Test order (matters; pytest runs in file order):
  1. test_normal_user_repos_available           -- read-only available list
  2. test_normal_user_activate_workspace        -- activate
  3. test_normal_user_repos_list                -- list (depends on activated)
  4. test_normal_user_sync_workspace            -- sync  (depends on activated)
  5. test_normal_user_cannot_switch_branch_on_global_alias -- AC2 negative
  6. test_normal_user_deactivate_workspace      -- MUST RUN LAST (tears down)
"""

from __future__ import annotations

import secrets
import string
import subprocess
import uuid
from pathlib import Path
from subprocess import CompletedProcess
from typing import Generator

import httpx
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import (
    GIT_SUBPROCESS_TIMEOUT,
    login,
    rest_call,
    run_cidx,
    wait_for_repo_activation,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _make_policy_password(length: int = 16) -> str:
    """Generate a password matching CIDX server policy (>=12, mixed case + digit + special)."""
    if length < 12:
        raise ValueError(f"length must be at least 12, got {length}")
    rng = secrets.SystemRandom()
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    pool = string.ascii_letters + string.digits + "!@#$%^&*"
    filler = [secrets.choice(pool) for _ in range(length - 4)]
    chars = required + filler
    rng.shuffle(chars)
    return "".join(chars)


def _assert_ok(result: CompletedProcess[str], label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _init_git_workspace(workspace: Path, remote_url: str) -> None:
    """Clone seed repo into workspace so cidx remote-mode branch matching works."""
    cwd = str(workspace)
    subprocess.run(
        ["git", "clone", remote_url, "."],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    branch_check = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
    )
    if not branch_check.stdout.strip():
        remote_refs = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/remotes/origin/",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT,
        )
        remote_branches = [
            b.strip()
            for b in remote_refs.stdout.splitlines()
            if b.strip() and not b.strip().endswith("/HEAD")
        ]
        if not remote_branches:
            raise RuntimeError(
                f"git clone of {remote_url} left workspace detached "
                "with no remote branches — cannot recover a named branch."
            )
        remote_ref = remote_branches[0]
        local_name = remote_ref.split("/", 1)[-1]
        subprocess.run(
            ["git", "checkout", "-b", local_name, "--track", remote_ref],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT,
        )
    subprocess.run(
        ["git", "config", "user.name", "CIDX E2E Normal"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "normal@cidx.test"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    with open(workspace / ".gitignore", "a", encoding="utf-8") as fh:
        fh.write("\n.code-indexer/\n")


# ---------------------------------------------------------------------------
# Module-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def normal_user(
    e2e_http_client: httpx.Client,
    e2e_admin_token: str,
) -> Generator[tuple[str, str], None, None]:
    """Create a NORMAL_USER via admin REST and yield (username, password). Delete on teardown."""
    username = f"normal_{uuid.uuid4().hex[:8]}"
    password = _make_policy_password()

    create_resp = rest_call(
        e2e_http_client,
        "POST",
        "/api/admin/users",
        token=e2e_admin_token,
        json={
            "username": username,
            "password": password,
            "role": "normal_user",
        },
    )
    assert create_resp.status_code in (200, 201), (
        f"Create normal user failed: {create_resp.status_code} {create_resp.text}"
    )

    yield username, password

    rest_call(
        e2e_http_client,
        "DELETE",
        f"/api/admin/users/{username}",
        token=e2e_admin_token,
    )


@pytest.fixture(scope="module")
def normal_user_token(e2e_server_url: str, normal_user: tuple[str, str]) -> str:
    """Login as the normal user and return their JWT (for direct REST calls)."""
    username, password = normal_user
    return login(e2e_server_url, username, password)


@pytest.fixture(scope="module")
def normal_user_workspace(
    e2e_server_url: str,
    e2e_config: E2EConfig,
    e2e_cli_env: dict[str, str],
    normal_user: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """Yield a tmp dir initialised in remote mode and authenticated as the normal user."""
    workspace = tmp_path_factory.mktemp("normal_user_ws", numbered=False)
    _init_git_workspace(
        workspace,
        remote_url=str(e2e_config.seed_cache_dir / "markupsafe"),
    )

    username, password = normal_user
    init_result = run_cidx(
        "init",
        "--remote",
        e2e_server_url,
        "--username",
        username,
        "--password",
        password,
        cwd=str(workspace),
        env=e2e_cli_env,
    )
    _assert_ok(init_result, f"cidx init --remote (normal user {username})")
    return workspace


# ---------------------------------------------------------------------------
# AC1 tests — normal user can perform workspace operations
# ---------------------------------------------------------------------------


def test_normal_user_repos_available(
    normal_user_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """AC1: normal user can invoke `cidx repos available` (rc=0, non-empty).

    Note: server-side `repos available` is filtered by group membership.
    A freshly created normal user with no group memberships will not see
    repos like ``markupsafe`` that are not associated with a universal
    group; that is correct pre-existing behavior, not an #981 regression.
    The AC1 capability validated here is that the command itself is
    permitted for normal users — independent of group setup.
    """
    result = run_cidx(
        "repos",
        "available",
        cwd=str(normal_user_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos available (normal user)")
    assert result.stdout.strip(), "cidx repos available returned empty output"


def test_normal_user_activate_workspace(
    normal_user_workspace: Path,
    registered_golden_repo: str,
    normal_user_token: str,
    e2e_http_client: httpx.Client,
    e2e_config: E2EConfig,
    e2e_cli_env: dict[str, str],
) -> None:
    """AC1: normal user can activate a golden repo into their own workspace."""
    result = run_cidx(
        "repos",
        "activate",
        registered_golden_repo,
        cwd=str(normal_user_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos activate (normal user)")
    wait_for_repo_activation(
        e2e_http_client,
        alias=registered_golden_repo,
        token=normal_user_token,
        timeout=e2e_config.repo_activation_timeout,
    )


def test_normal_user_repos_list(
    normal_user_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """AC1: normal user sees their own activated workspace in repos list."""
    result = run_cidx(
        "repos", "list", cwd=str(normal_user_workspace), env=e2e_cli_env
    )
    _assert_ok(result, "cidx repos list (normal user)")
    assert registered_golden_repo in result.stdout, (
        f"Expected '{registered_golden_repo}' in normal-user repos list:\n"
        f"{result.stdout}"
    )


def test_normal_user_sync_workspace(
    normal_user_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """AC1: normal user can sync their own workspace (rc 0 or 1 acceptable)."""
    result = run_cidx(
        "repos",
        "sync",
        registered_golden_repo,
        cwd=str(normal_user_workspace),
        env=e2e_cli_env,
    )
    assert result.returncode in (0, 1), (
        f"cidx repos sync (normal user) unexpected rc={result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC2 negative test — normal user blocked from *-global branch switch
# ---------------------------------------------------------------------------


def test_normal_user_cannot_switch_branch_on_global_alias(
    e2e_http_client: httpx.Client,
    normal_user_token: str,
) -> None:
    """AC2: normal user calling switch-branch on a *-global alias gets 403.

    The role guard fires at the very top of the switch_branch handler before
    any state lookup, so a non-existent global alias still returns 403 —
    proving the role gate is active and the alias-suffix check rejects normal
    users regardless of whether the global alias exists.
    """
    response = rest_call(
        e2e_http_client,
        "POST",
        "/api/activated-repos/anything-global/branch",
        token=normal_user_token,
        json={"branch_name": "main"},
    )
    assert response.status_code == 403, (
        f"Expected 403 for normal user on *-global branch switch, got "
        f"{response.status_code}: {response.text}"
    )
    body = response.json()
    detail = body.get("detail", "")
    if isinstance(detail, dict):
        detail = str(detail)
    assert "global" in detail.lower() or "admin" in detail.lower(), (
        f"Expected 'global' or 'admin' in the 403 detail message, got: {body}"
    )


# ---------------------------------------------------------------------------
# AC1 cleanup test — MUST RUN LAST (tears down workspace state)
# ---------------------------------------------------------------------------


def test_normal_user_deactivate_workspace(
    normal_user_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """AC1: normal user can deactivate their own workspace.

    Runs LAST so earlier tests still see an activated repo.
    """
    result = run_cidx(
        "repos",
        "deactivate",
        registered_golden_repo,
        "--force",
        cwd=str(normal_user_workspace),
        env=e2e_cli_env,
        stdin_input="y\n",
    )
    _assert_ok(result, "cidx repos deactivate (normal user)")
