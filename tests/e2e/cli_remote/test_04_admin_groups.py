"""
Phase 4 E2E tests: CLI remote admin group management (Story #706 AC2).

Tests exercise real CLI subprocess calls against the live E2E server.
No mocking -- all assertions are based on actual process exit codes and
stdout/stderr output.

All tests use the ``authenticated_workspace`` fixture (session-scoped) so
the workspace is already initialised in remote mode and authenticated as
admin before any test runs.

The ``created_group`` fixture (module-scoped) guarantees that a test group
exists before any test in this module that needs it, and deletes it on
teardown.  The group ID (integer) is captured from the JSON output of
``cidx admin groups create --json`` and used in all dependent operations.

Self-contained tests (create, delete) wrap post-creation logic in
``try/finally`` so cleanup always runs after ``group_id`` is captured,
regardless of assertion failures.

Test functions (6):
  test_admin_groups_create     -- create a new group (self-contained)
  test_admin_groups_list       -- list groups, verify created group visible
  test_admin_groups_show       -- show details for created group
  test_admin_groups_update     -- update group description and verify via show
  test_admin_groups_add_member -- add admin user as member and verify via show
  test_admin_groups_delete     -- delete a self-contained group (self-contained)
"""

from __future__ import annotations

import json
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


def _parse_group_id(result: CompletedProcess[str], context: str) -> int:
    """Parse and return the integer group ID from a JSON create response.

    Raises ``pytest.fail`` on any parse error so failures are explicit.
    """
    try:
        data = json.loads(result.stdout)
        return int(data["group_id"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        pytest.fail(
            f"{context}: could not parse group ID from JSON output: {exc}\n"
            f"stdout: {result.stdout}"
        )


def _remove_all_members_from_group(
    group_id: int,
    workspace: Path,
    cli_env: dict[str, str],
    *,
    label: str = "cleanup",
) -> None:
    """Remove all members from a group by reassigning them to the default admins group.

    Uses the 1:1 user-to-group relationship: calling add-member on the "admins"
    default group moves each user out of the test group automatically.  This is
    necessary before delete, because cidx admin groups delete refuses when active
    members exist (GroupHasUsersError on the server side).

    Each failure path emits an explicit warning via pytest.warns-style print so the
    reason for any skipped removal is visible in test output.  After this function
    returns (regardless of outcome), the caller attempts the delete and surfaces any
    remaining error through its own AssertionError.
    """
    import warnings

    # Get the current member list from the group
    show_result = run_cidx(
        "admin", "groups", "show", str(group_id),
        "--json",
        cwd=str(workspace),
        env=cli_env,
    )
    if show_result.returncode != 0:
        warnings.warn(
            f"{label}: could not fetch group {group_id} members before delete "
            f"(rc={show_result.returncode}): {show_result.stdout}{show_result.stderr}",
            stacklevel=2,
        )
        return

    try:
        data = json.loads(show_result.stdout)
        members: list[str] = data.get("members", [])
    except (json.JSONDecodeError, KeyError) as exc:
        warnings.warn(
            f"{label}: could not parse members from group {group_id} show output: {exc}\n"
            f"stdout: {show_result.stdout}",
            stacklevel=2,
        )
        return

    if not members:
        return  # Nothing to remove -- no warning needed

    # Find the ID of the default "admins" group so we can move users back to it
    list_result = run_cidx(
        "admin", "groups", "list",
        "--json",
        cwd=str(workspace),
        env=cli_env,
    )
    admins_group_id: int | None = None
    if list_result.returncode == 0:
        try:
            groups_data = json.loads(list_result.stdout)
            for grp in groups_data.get("groups", []):
                if grp.get("name") == "admins":
                    admins_group_id = int(grp["id"])
                    break
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            warnings.warn(
                f"{label}: could not parse groups list to find admins group: {exc}\n"
                f"stdout: {list_result.stdout}",
                stacklevel=2,
            )
    else:
        warnings.warn(
            f"{label}: cidx admin groups list failed "
            f"(rc={list_result.returncode}): {list_result.stdout}{list_result.stderr}",
            stacklevel=2,
        )

    if admins_group_id is None:
        warnings.warn(
            f"{label}: admins group not found in list output -- "
            f"cannot move members {members} out of group {group_id} before delete",
            stacklevel=2,
        )
        return

    for member in members:
        move_result = run_cidx(
            "admin", "groups", "add-member", str(admins_group_id),
            "--user", member,
            cwd=str(workspace),
            env=cli_env,
        )
        if move_result.returncode != 0:
            warnings.warn(
                f"{label}: failed to move user '{member}' from group {group_id} "
                f"to admins group {admins_group_id} "
                f"(rc={move_result.returncode}): "
                f"{move_result.stdout}{move_result.stderr}",
                stacklevel=2,
            )


def _delete_group_best_effort(
    group_id: int,
    workspace: Path,
    cli_env: dict[str, str],
    *,
    label: str = "cleanup",
) -> None:
    """Delete a group by ID, accepting success or 'already gone' outcomes.

    Moves any active members back to the default admins group first (required
    because the server rejects delete when active members exist).

    Any other failure is re-raised so cleanup errors are not silently discarded.
    """
    # Move members out before attempting delete
    _remove_all_members_from_group(group_id, workspace, cli_env, label=label)

    result = run_cidx(
        "admin",
        "groups",
        "delete",
        str(group_id),
        "--confirm",
        cwd=str(workspace),
        env=cli_env,
    )
    if result.returncode == 0:
        return
    combined = (result.stdout + result.stderr).lower()
    not_found_indicators = {"not found", "does not exist", "no group", "404"}
    if any(indicator in combined for indicator in not_found_indicators):
        return
    raise AssertionError(
        f"{label}: cidx admin groups delete {group_id} failed unexpectedly "
        f"(rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Module-scoped fixture: creates a test group and yields (group_id, group_name)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def created_group(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> Generator[tuple[int, str], None, None]:
    """Create a test group, yield (group_id, group_name), then delete on teardown."""
    group_name = f"e2egrp_{uuid.uuid4().hex[:8]}"

    create_result = run_cidx(
        "admin",
        "groups",
        "create",
        "--name",
        group_name,
        "--description",
        "E2E test group",
        "--json",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    assert create_result.returncode == 0, (
        f"created_group fixture: cidx admin groups create failed "
        f"(rc={create_result.returncode}):\n"
        f"stdout: {create_result.stdout}\nstderr: {create_result.stderr}"
    )

    group_id = _parse_group_id(create_result, "created_group fixture")

    yield group_id, group_name

    _delete_group_best_effort(
        group_id,
        authenticated_workspace,
        e2e_cli_env,
        label="created_group fixture teardown",
    )


# ---------------------------------------------------------------------------
# Admin group tests
# ---------------------------------------------------------------------------


def test_admin_groups_create(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx admin groups create --name <name> exits 0 and returns parseable JSON with id.

    Self-contained: creates and deletes its own group.  Cleanup is guaranteed
    via ``try/finally`` once the group ID has been successfully parsed.
    """
    group_name = f"e2ecreate_{uuid.uuid4().hex[:8]}"

    create_result = run_cidx(
        "admin",
        "groups",
        "create",
        "--name",
        group_name,
        "--json",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(create_result, f"cidx admin groups create --name {group_name}")

    group_id = _parse_group_id(create_result, "test_admin_groups_create")

    try:
        # No additional assertions needed here beyond rc=0 and parseable ID;
        # the presence of a valid integer ID confirms the create contract.
        assert group_id > 0, f"Expected positive group ID, got {group_id}"
    finally:
        _delete_group_best_effort(
            group_id,
            authenticated_workspace,
            e2e_cli_env,
            label="test_admin_groups_create cleanup",
        )


def test_admin_groups_list(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_group: tuple[int, str],
) -> None:
    """cidx admin groups list exits 0 and contains the created group name."""
    group_id, group_name = created_group
    result = run_cidx(
        "admin",
        "groups",
        "list",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, "cidx admin groups list")
    assert result.stdout.strip(), "cidx admin groups list returned empty output"
    assert group_name in result.stdout, (
        f"Expected '{group_name}' in groups list output but got:\n{result.stdout}"
    )


def test_admin_groups_show(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_group: tuple[int, str],
) -> None:
    """cidx admin groups show <group_id> exits 0 and contains the group name."""
    group_id, group_name = created_group
    result = run_cidx(
        "admin",
        "groups",
        "show",
        str(group_id),
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx admin groups show {group_id}")
    assert group_name in result.stdout, (
        f"Expected '{group_name}' in show output but got:\n{result.stdout}"
    )


def test_admin_groups_update(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_group: tuple[int, str],
) -> None:
    """cidx admin groups update <group_id> --description exits 0 and persists the change."""
    group_id, _ = created_group
    new_description = "Updated E2E test group description"

    update_result = run_cidx(
        "admin",
        "groups",
        "update",
        str(group_id),
        "--description",
        new_description,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(update_result, f"cidx admin groups update {group_id}")

    # Verify the update was persisted by reading back the group
    show_result = run_cidx(
        "admin",
        "groups",
        "show",
        str(group_id),
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(show_result, f"cidx admin groups show {group_id} (post-update)")
    assert new_description in show_result.stdout, (
        f"Expected updated description '{new_description}' in show output "
        f"after update but got:\n{show_result.stdout}"
    )


def test_admin_groups_add_member(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
    created_group: tuple[int, str],
) -> None:
    """cidx admin groups add-member <group_id> --user admin exits 0 and admin appears in members."""
    group_id, _ = created_group

    add_result = run_cidx(
        "admin",
        "groups",
        "add-member",
        str(group_id),
        "--user",
        "admin",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(add_result, f"cidx admin groups add-member {group_id} --user admin")

    # Verify the membership was persisted by reading back the group
    show_result = run_cidx(
        "admin",
        "groups",
        "show",
        str(group_id),
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(show_result, f"cidx admin groups show {group_id} (post-add-member)")
    assert "admin" in show_result.stdout.lower(), (
        f"Expected 'admin' to appear as a member in show output "
        f"after add-member but got:\n{show_result.stdout}"
    )


def test_admin_groups_delete(
    authenticated_workspace: Path,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx admin groups delete <group_id> --confirm exits 0.

    Self-contained: creates its own group so this test does not interfere
    with the shared ``created_group`` fixture lifecycle.  Uses ``try/finally``
    to guarantee cleanup runs even when the delete assertion fails (e.g. if
    the server returns a non-zero exit for an unexpected reason).
    """
    group_name = f"e2edelete_{uuid.uuid4().hex[:8]}"

    create_result = run_cidx(
        "admin",
        "groups",
        "create",
        "--name",
        group_name,
        "--json",
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(
        create_result,
        f"test_admin_groups_delete setup: cidx admin groups create --name {group_name}",
    )

    group_id = _parse_group_id(create_result, "test_admin_groups_delete setup")

    delete_failed = False
    try:
        delete_result = run_cidx(
            "admin",
            "groups",
            "delete",
            str(group_id),
            "--confirm",
            cwd=str(authenticated_workspace),
            env=e2e_cli_env,
        )
        if delete_result.returncode != 0:
            delete_failed = True
            delete_error = (
                f"cidx admin groups delete {group_id} failed "
                f"(rc={delete_result.returncode}):\n"
                f"stdout: {delete_result.stdout}\nstderr: {delete_result.stderr}"
            )
    finally:
        if delete_failed:
            # Best-effort cleanup since the primary delete did not succeed
            _delete_group_best_effort(
                group_id,
                authenticated_workspace,
                e2e_cli_env,
                label="test_admin_groups_delete fallback cleanup",
            )

    if delete_failed:
        pytest.fail(delete_error)  # type: ignore[possibly-undefined]
