"""
Phase 4 E2E tests: CLI remote admin user management (Story #706 AC1).

Tests exercise real CLI subprocess calls against the live E2E server.
No mocking -- all assertions are based on actual process exit codes and
stdout/stderr output.

All tests use the ``authenticated_workspace`` fixture (session-scoped) so
the workspace is already initialised in remote mode and authenticated as
admin before any test runs.

The ``created_user`` fixture (module-scoped) guarantees that a test user
exists before any test in this module that needs it, and deletes it on
teardown.  All passwords are generated at runtime -- no hardcoded credentials.

Cleanup paths explicitly handle ``run_cidx`` return values: they assert
success or accept only the documented "already gone" failure mode to avoid
silent discard of unexpected errors.

Test functions (6):
  test_admin_users_create          -- create a new user account (self-contained)
  test_admin_users_list            -- list users, verify created user visible
  test_admin_users_show            -- show details for created user
  test_admin_users_update          -- update user role
  test_admin_users_change_password -- change user password
  test_admin_users_delete          -- delete a self-contained user (self-contained)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from subprocess import CompletedProcess
from typing import Generator

import pytest

from tests.e2e.helpers import run_cidx


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _assert_ok(result: CompletedProcess[str], label: str) -> None:
    """Assert that ``result.returncode == 0`` with an informative failure message."""
    assert result.returncode == 0, (
        f"{label} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def _delete_user_best_effort(
    username: str,
    workspace: Path,
    cli_env: dict[str, str],
    *,
    label: str = "cleanup",
) -> None:
    """Delete a user, accepting success or 'already gone' (not-found) outcomes.

    Any other failure (unexpected error, server error) is re-raised so that
    cleanup errors are not silently discarded.
    """
    result = run_cidx(
        "admin", "users", "delete", username,
        "--force",
        cwd=str(workspace),
        env=cli_env,
    )
    if result.returncode == 0:
        return
    combined = (result.stdout + result.stderr).lower()
    not_found_indicators = {"not found", "does not exist", "no user", "404"}
    if any(indicator in combined for indicator in not_found_indicators):
        # User was already deleted (or never existed) -- acceptable in teardown
        return
    raise AssertionError(
        f"{label}: cidx admin users delete {username!r} failed unexpectedly "
        f"(rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Module-scoped fixture: creates a test user and yields (username, password)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def created_user(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> Generator[tuple[str, str], None, None]:
    """Create a test user, yield (username, password), then delete on teardown.

    Passwords are generated at runtime to avoid hardcoding credentials in source.
    Teardown uses ``_delete_user_best_effort`` to handle explicit error modes
    rather than silently ignoring the cleanup result.
    """
    username = f"e2euser_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex  # random, satisfies basic length requirements

    create_result = run_cidx(
        "admin", "users", "create", username,
        "--password", password,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    assert create_result.returncode == 0, (
        f"created_user fixture: cidx admin users create failed "
        f"(rc={create_result.returncode}):\n"
        f"stdout: {create_result.stdout}\nstderr: {create_result.stderr}"
    )

    yield username, password

    # Teardown: delete the user with explicit error handling (not silent discard)
    _delete_user_best_effort(
        username, authenticated_workspace, e2e_cli_env,
        label="created_user fixture teardown",
    )


# ---------------------------------------------------------------------------
# Admin user tests
# ---------------------------------------------------------------------------


def test_admin_users_create(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx admin users create <username> --password <pass> exits 0.

    Self-contained: creates and deletes its own user within this test.
    """
    username = f"e2ecreate_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex

    result = run_cidx(
        "admin", "users", "create", username,
        "--password", password,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx admin users create {username}")

    # Cleanup: explicitly handle return value, not silent discard
    _delete_user_best_effort(
        username, authenticated_workspace, e2e_cli_env,
        label="test_admin_users_create cleanup",
    )


def test_admin_users_list(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_user: tuple[str, str],
) -> None:
    """cidx admin users list exits 0 and contains both admin and the created user."""
    username, _ = created_user
    result = run_cidx(
        "admin", "users", "list",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, "cidx admin users list")
    assert result.stdout.strip(), "cidx admin users list returned empty output"
    # The fixture-created username must appear in the listing
    assert username in result.stdout, (
        f"Expected '{username}' in users list output but got:\n{result.stdout}"
    )
    # The admin user is always present
    assert "admin" in result.stdout.lower(), (
        f"Expected 'admin' in users list output but got:\n{result.stdout}"
    )


def test_admin_users_show(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_user: tuple[str, str],
) -> None:
    """cidx admin users show <username> exits 0 for the created test user."""
    username, _ = created_user
    result = run_cidx(
        "admin", "users", "show", username,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx admin users show {username}")
    assert username in result.stdout, (
        f"Expected '{username}' in show output but got:\n{result.stdout}"
    )


def test_admin_users_update(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_user: tuple[str, str],
) -> None:
    """cidx admin users update <username> --role power_user exits 0."""
    username, _ = created_user
    result = run_cidx(
        "admin", "users", "update", username,
        "--role", "power_user",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx admin users update {username}")


def test_admin_users_change_password(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_user: tuple[str, str],
) -> None:
    """cidx admin users change-password <username> --password <new> exits 0."""
    username, _ = created_user
    new_password = uuid.uuid4().hex  # generate at runtime, no hardcoded credentials

    result = run_cidx(
        "admin", "users", "change-password", username,
        "--password", new_password,
        "--force",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx admin users change-password {username}")


def test_admin_users_delete(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx admin users delete <username> --force exits 0.

    Self-contained: creates its own user so this test does not interfere
    with the shared ``created_user`` fixture lifecycle.
    """
    username = f"e2edelete_{uuid.uuid4().hex[:8]}"
    password = uuid.uuid4().hex

    create_result = run_cidx(
        "admin", "users", "create", username,
        "--password", password,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(
        create_result,
        f"test_admin_users_delete setup: cidx admin users create {username}",
    )

    delete_result = run_cidx(
        "admin", "users", "delete", username,
        "--force",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(delete_result, f"cidx admin users delete {username}")
