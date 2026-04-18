"""
Phase 4 E2E tests: CLI remote repository management (Story #705 AC3).

Tests exercise real CLI subprocess calls against the live E2E server.
No mocking -- all assertions are based on actual process exit codes and
stdout/stderr output.

Fixture dependency rules:
  test_repos_available  -- uses ``registered_golden_repo`` (no activation needed
                           for a repo to appear in the available list)
  test_repos_deactivate -- uses ``registered_golden_repo`` and performs its own
                           independent activate (rc=0 required) then deactivate
                           to avoid mutating shared session state
  all other 10 tests    -- use ``activated_golden_repo`` (session-scoped)

Private helper:
  _assert_ok -- asserts rc=0 with an informative failure message

Test functions (12):
  test_repos_available     -- browse available golden repos
  test_repos_activate      -- verify activate is idempotent (rc=0)
  test_repos_list          -- list activated repos
  test_repos_status        -- show repo status overview
  test_repos_info          -- show detailed repo info
  test_repos_files         -- browse repo files
  test_repos_cat           -- view file contents
  test_repos_switch_branch -- skipped (requires known branch)
  test_repos_sync          -- sync repo (rc 0 or 1 accepted)
  test_repos_sync_status   -- show sync status
  test_query_via_server    -- cidx query returns results
  test_repos_deactivate    -- independent activate (rc=0) + deactivate
"""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.e2e.helpers import run_cidx

# Alias used consistently across all repo tests in this module
MARKUPSAFE_ALIAS = "markupsafe"

# A file known to exist at the root of markupsafe 2.1.5
KNOWN_ROOT_FILE = "MANIFEST.in"


# ---------------------------------------------------------------------------
# Private helper
# ---------------------------------------------------------------------------


def _assert_ok(result: CompletedProcess[str], label: str) -> None:
    """Assert that ``result.returncode == 0`` with an informative failure message."""
    assert result.returncode == 0, (
        f"{label} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Repository management tests
# ---------------------------------------------------------------------------


def test_repos_available(
    authenticated_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos available exits 0 and lists the markupsafe golden repo.

    Uses ``registered_golden_repo`` because a repo need not be activated
    to appear in the available list.
    """
    result = run_cidx("repos", "available", cwd=str(authenticated_workspace), env=e2e_cli_env)
    _assert_ok(result, "cidx repos available")
    assert MARKUPSAFE_ALIAS in result.stdout, (
        f"Expected '{MARKUPSAFE_ALIAS}' in repos available output but got:\n{result.stdout}"
    )


def test_repos_activate(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos activate markupsafe exits 0 when re-run on an already-active repo."""
    result = run_cidx(
        "repos", "activate", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos activate")


def test_repos_list(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos list exits 0 and contains markupsafe."""
    result = run_cidx("repos", "list", cwd=str(authenticated_workspace), env=e2e_cli_env)
    _assert_ok(result, "cidx repos list")
    assert MARKUPSAFE_ALIAS in result.stdout, (
        f"Expected '{MARKUPSAFE_ALIAS}' in repos list output but got:\n{result.stdout}"
    )


def test_repos_status(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos status exits 0."""
    result = run_cidx("repos", "status", cwd=str(authenticated_workspace), env=e2e_cli_env)
    _assert_ok(result, "cidx repos status")


def test_repos_info(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos info markupsafe exits 0."""
    result = run_cidx(
        "repos", "info", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos info")


def test_repos_files(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos files markupsafe exits 0 and returns non-empty output."""
    result = run_cidx(
        "repos", "files", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos files")
    assert result.stdout.strip(), "cidx repos files returned empty output"


def test_repos_cat(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos cat markupsafe MANIFEST.in exits 0 and returns file contents."""
    result = run_cidx(
        "repos", "cat", MARKUPSAFE_ALIAS, KNOWN_ROOT_FILE,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx repos cat {KNOWN_ROOT_FILE}")
    assert result.stdout.strip(), f"cidx repos cat returned empty output for {KNOWN_ROOT_FILE}"


@pytest.mark.skip(reason="requires a known branch name in the markupsafe golden repo")
def test_repos_switch_branch(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos switch-branch markupsafe main exits 0 (skipped -- branch unknown)."""
    result = run_cidx(
        "repos", "switch-branch", MARKUPSAFE_ALIAS, "main",
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos switch-branch")


def test_repos_sync(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos sync markupsafe exits 0 or 1 (nothing-to-sync is acceptable)."""
    result = run_cidx(
        "repos", "sync", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    assert result.returncode in (0, 1), (
        f"cidx repos sync returned unexpected rc={result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_repos_sync_status(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos sync-status markupsafe exits 0."""
    result = run_cidx(
        "repos", "sync-status", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos sync-status")


def test_query_via_server(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx query against remote server returns non-empty results.

    Requires ``activated_golden_repo`` to ensure markupsafe is active.
    """
    result = run_cidx(
        "query", "escape",
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx query")
    assert result.stdout.strip(), "cidx query returned empty output"


def test_repos_deactivate(
    authenticated_workspace: Path,
    registered_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx repos deactivate markupsafe exits 0.

    Activates the repo independently (rc=0 required) before deactivating
    to avoid mutating the shared ``activated_golden_repo`` session state.
    """
    activate_result = run_cidx(
        "repos", "activate", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(activate_result, "cidx repos activate (pre-deactivate setup)")

    result = run_cidx(
        "repos", "deactivate", MARKUPSAFE_ALIAS,
        cwd=str(authenticated_workspace), env=e2e_cli_env,
    )
    _assert_ok(result, "cidx repos deactivate")
