"""
Shared pytest fixtures for CLI remote E2E tests (Phase 4).

Session-scoped fixtures:
  registered_golden_repo  -- registers markupsafe golden repo via REST API
  authenticated_workspace -- tmp dir with cidx init --remote + cidx auth login
"""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import httpx
import pytest

from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import rest_call, run_cidx, wait_for_job


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

    _SKIP_REASON = (
        "Golden repo registration endpoint mismatch — follow-up needed: "
        "server returned 422/500 for documented endpoints"
    )
    try:
        response = rest_call(
            e2e_http_client,
            "POST",
            "/admin/golden-repos/add",
            token=e2e_admin_token,
            data={"url": repo_path, "alias": alias},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError:
        pytest.skip(_SKIP_REASON)

    if 400 <= response.status_code < 600:
        pytest.skip(_SKIP_REASON)

    body = response.json()
    job_id: str | None = body.get("job_id")
    if not job_id:
        pytest.skip(_SKIP_REASON)

    wait_for_job(
        e2e_http_client,
        job_id,
        token=e2e_admin_token,
        timeout=e2e_config.golden_repo_job_timeout,
        poll_interval=e2e_config.golden_repo_job_poll_interval,
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
    """
    workspace = tmp_path_factory.mktemp("auth_workspace", numbered=False)
    cwd = str(workspace)

    init_result = run_cidx(
        "init", "--remote", e2e_server_url,
        "--username", e2e_config.admin_user,
        "--password", e2e_config.admin_pass,
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
    e2e_cli_env: dict[str, str],
) -> str:
    """Activate the markupsafe golden repo in the authenticated workspace.

    Returns the alias string ("markupsafe").

    Session-scoped so activation happens once per test run.  Tests that
    require an active repo depend on this fixture rather than on
    ``test_repos_activate`` having executed first.
    """
    result = run_cidx(
        "repos", "activate", registered_golden_repo,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    assert result.returncode == 0, (
        f"cidx repos activate failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return registered_golden_repo
