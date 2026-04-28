"""
Shared pytest fixtures for CLI remote E2E tests (Phase 4).

Session-scoped fixtures:
  registered_golden_repo  -- registers markupsafe golden repo via REST API
  authenticated_workspace -- tmp dir with cidx init --remote + cidx auth login
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Generator

import httpx
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import (
    GIT_SUBPROCESS_TIMEOUT,
    rest_call,
    run_cidx,
    wait_for_job,
    wait_for_repo_activation,
)


# ---------------------------------------------------------------------------
# Private: workspace git initialisation (Bug 4)
# ---------------------------------------------------------------------------


def _init_git_workspace(workspace: Path, remote_url: str) -> None:
    """Clone ``remote_url`` into ``workspace`` so branch matching works for cidx query.

    ``cidx query`` in remote mode uses exact-branch matching: it reads the
    workspace's current branch via ``git branch --show-current`` and compares
    it against the branches available in the activated remote repository.  A
    bare ``git init`` (no commits) leaves the workspace on an *unborn* branch,
    so ``git branch --show-current`` returns empty → branch detection returns
    ``None`` → repository linking fails with ``BranchMatchingError``.

    Using ``git clone <seed_path> .`` ensures the workspace has real commits,
    is on the same branch as the seed (e.g. ``main``), and already has
    ``remote.origin.url`` set to the seed path — exactly what repository
    linking needs.

    Raises ``subprocess.CalledProcessError`` on any non-zero git exit so the
    fixture fails loudly rather than proceeding with a broken repo.
    """
    cwd = str(workspace)
    # Clone seed into the (empty) workspace directory; sets origin automatically.
    subprocess.run(
        ["git", "clone", remote_url, "."],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )

    # The seed repo may be in detached HEAD (e.g. after a bare fetch or
    # checkout of a specific commit).  git clone propagates that state, leaving
    # the workspace also detached.  cidx query's branch-matching logic calls
    # ``git branch --show-current`` and treats empty output as "no branch" →
    # BranchMatchingError.  Fix: if detached, find the first named remote
    # tracking branch and check it out locally.  Fail loudly if none exists so
    # the fixture never silently passes a broken workspace.
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
                f"git clone of {remote_url} left workspace in detached HEAD "
                "with no remote tracking branches — cannot recover a named branch."
            )
        remote_ref = remote_branches[0]  # e.g. "origin/main"
        local_name = remote_ref.split("/", 1)[-1]  # e.g. "main"
        subprocess.run(
            ["git", "checkout", "-b", local_name, "--track", remote_ref],
            cwd=cwd,
            capture_output=True,
            check=True,
            timeout=GIT_SUBPROCESS_TIMEOUT,
        )

    # Minimal identity config (scoped to this repo)
    subprocess.run(
        ["git", "config", "user.name", "CIDX E2E"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "e2e@cidx.test"],
        cwd=cwd,
        timeout=GIT_SUBPROCESS_TIMEOUT,
        check=True,
    )
    # Append cidx metadata exclusion (markupsafe already has its own .gitignore)
    with open(workspace / ".gitignore", "a", encoding="utf-8") as fh:
        fh.write("\n.code-indexer/\n")


# ---------------------------------------------------------------------------
# registered_golden_repo
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def registered_golden_repo(
    e2e_config: E2EConfig,
    e2e_admin_token: str,
    e2e_http_client: httpx.Client,
) -> str:
    """Register the markupsafe golden repo on the server and wait for indexing.

    Returns the alias string ("markupsafe").

    Uses REST POST /api/admin/golden-repos with JSON body containing
    ``repo_url`` and ``alias``.  The response must contain ``job_id``
    (JobResponse contract) -- fails immediately if absent.

    Timeout and poll interval are read from ``e2e_config``, which sources
    them from ``E2E_GOLDEN_REPO_JOB_TIMEOUT`` and
    ``E2E_GOLDEN_REPO_JOB_POLL_INTERVAL`` environment variables.
    """
    repo_path = str(e2e_config.seed_cache_dir / "markupsafe")
    alias = "markupsafe"

    response = rest_call(
        e2e_http_client,
        "POST",
        "/api/admin/golden-repos",
        token=e2e_admin_token,
        json={"repo_url": repo_path, "alias": alias},
    )
    response.raise_for_status()

    body = response.json()
    job_id: str = body["job_id"]

    job_status = wait_for_job(
        e2e_http_client,
        job_id,
        token=e2e_admin_token,
        timeout=e2e_config.golden_repo_job_timeout,
        poll_interval=e2e_config.golden_repo_job_poll_interval,
    )
    # Messi Rule #13: fail loudly if the async registration didn't succeed.
    # wait_for_job() returns on ANY terminal state (completed/failed/cancelled),
    # so we must assert "completed" explicitly or downstream tests see a
    # phantom successful registration and fail in confusing ways.
    assert job_status["status"] == "completed", (
        f"Golden repo registration job did not complete successfully:\n{job_status}"
    )

    return alias


# ---------------------------------------------------------------------------
# authenticated_workspace
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def authenticated_workspace(
    e2e_server_url: str,
    e2e_config: E2EConfig,
    e2e_cli_env: dict[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[Path, None, None]:
    """Yield a temporary directory initialised in remote mode and authenticated.

    Runs ``cidx init --remote <url> --username <user> --password <pass>``.
    The ``--username`` and ``--password`` flags are required by the CLI when
    using ``--remote``; they perform authentication as part of initialisation.

    Before ``cidx init``, the workspace is initialised as a git repo whose
    ``remote.origin.url`` matches the markupsafe seed path used for the
    golden-repo registration.  Without this, ``cidx query`` in remote mode
    fails with ``RepositoryLinkingError: Current directory is not a git
    repository`` (Bug 4).
    """
    workspace = tmp_path_factory.mktemp("auth_workspace", numbered=False)
    cwd = str(workspace)

    _init_git_workspace(
        workspace,
        remote_url=str(e2e_config.seed_cache_dir / "markupsafe"),
    )

    init_result = run_cidx(
        "init",
        "--remote",
        e2e_server_url,
        "--username",
        e2e_config.admin_user,
        "--password",
        e2e_config.admin_pass,
        cwd=cwd,
        env=e2e_cli_env,
    )
    assert init_result.returncode == 0, (
        f"cidx init --remote failed (rc={init_result.returncode}):\n"
        f"stdout: {init_result.stdout}\nstderr: {init_result.stderr}"
    )

    yield workspace


# ---------------------------------------------------------------------------
# activated_golden_repo
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def activated_golden_repo(
    authenticated_workspace: Path,
    registered_golden_repo: str,
    e2e_config: E2EConfig,
    e2e_http_client: httpx.Client,
    e2e_admin_token: str,
    e2e_cli_env: dict[str, str],
) -> str:
    """Activate the markupsafe golden repo in the authenticated workspace.

    Returns the alias string ("markupsafe").

    Session-scoped so activation happens once per test run.  Tests that
    require an active repo depend on this fixture rather than on
    ``test_repos_activate`` having executed first.

    After the CLI command returns rc=0, polls GET /api/repos/<alias> until
    the server reports 200 (activation complete) within
    ``e2e_config.repo_activation_timeout`` seconds — fixing the race where
    the server-side activation job is still running when the fixture returns.
    """
    result = run_cidx(
        "repos",
        "activate",
        registered_golden_repo,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    assert result.returncode == 0, (
        f"cidx repos activate failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    wait_for_repo_activation(
        e2e_http_client,
        alias=registered_golden_repo,
        token=e2e_admin_token,
        timeout=e2e_config.repo_activation_timeout,
    )
    return registered_golden_repo
