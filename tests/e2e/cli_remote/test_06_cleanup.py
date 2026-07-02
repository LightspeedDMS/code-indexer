"""
Phase 4 E2E tests: CLI remote cleanup (deactivation).

Runs AFTER test_05_git_files.py (alphabetical ordering ensures this).
Deactivation destroys the physical activated-repos directory, so it must
execute last — after all tests that depend on an active repository.

Test functions (1):
  test_repos_deactivate -- independent activate (rc=0) + deactivate
"""

from __future__ import annotations

from pathlib import Path


from tests.e2e.helpers import run_cidx

# Alias used consistently across all repo tests
MARKUPSAFE_ALIAS = "markupsafe"


def _assert_ok(result, label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_repos_deactivate(
    authenticated_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos deactivate markupsafe exits 0.

    Activates the repo independently (rc=0 required) before deactivating
    to avoid mutating the shared ``activated_golden_repo`` session state.

    This test runs in test_06_cleanup.py (after test_05_git_files.py) so
    the deactivation does not destroy the activated-repos directory before
    the git and file operation tests have a chance to run.
    """
    activate_result = run_cidx(
        "repos",
        "activate",
        MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(activate_result, "cidx repos activate (pre-deactivate setup)")

    result = run_cidx(
        "repos",
        "deactivate",
        MARKUPSAFE_ALIAS,
        "--force",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
        stdin_input="y\n",
    )
    _assert_ok(result, "cidx repos deactivate")
