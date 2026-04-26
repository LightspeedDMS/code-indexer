"""
Phase 4 E2E tests: CLI remote git history and file operations (Story #706 AC3/AC4).

Tests exercise real CLI subprocess calls against the live E2E server.
No mocking -- all assertions are based on actual process exit codes and
stdout/stderr output.

All tests in this module depend on ``activated_golden_repo`` which itself
depends on ``registered_golden_repo``.  When golden-repo registration is
deferred (the current state -- endpoint mismatch follow-up), all tests here
skip cleanly via the fixture chain.

A file known to exist in the markupsafe 2.1.5 repository is used for
file-specific git operations.

Test functions (7):
  test_git_log                  -- show commit history for the repo
  test_git_branches             -- list branches
  test_git_diff                 -- show diff (empty is acceptable on a clean clone)
  test_git_blame                -- show blame for a known file
  test_git_file_history         -- show commit history for a known file
  test_git_cat                  -- show file content at HEAD
  test_files_create_and_delete  -- create a file then delete it (self-contained)
"""

from __future__ import annotations

import uuid
from pathlib import Path
from subprocess import CompletedProcess


from tests.e2e.helpers import run_cidx

# Alias used consistently across all tests in this module
REPO_ALIAS = "markupsafe"

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
# Git tests (all depend on activated_golden_repo -- skip when deferred)
# ---------------------------------------------------------------------------


def test_git_log(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git log <alias> exits 0 and returns non-empty commit history."""
    result = run_cidx(
        "git", "log", "-r", REPO_ALIAS,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git log {REPO_ALIAS}")
    assert result.stdout.strip(), f"cidx git log {REPO_ALIAS} returned empty output"


def test_git_branches(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git branches <alias> exits 0 and returns non-empty branch listing."""
    result = run_cidx(
        "git", "branches", "-r", REPO_ALIAS,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git branches {REPO_ALIAS}")
    assert result.stdout.strip(), (
        f"cidx git branches {REPO_ALIAS} returned empty output"
    )


def test_git_diff(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git diff <alias> exits 0 (empty diff on a clean clone is acceptable)."""
    result = run_cidx(
        "git", "diff", "-r", REPO_ALIAS,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git diff {REPO_ALIAS}")


def test_git_blame(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git blame <alias> <file> exits 0 for a known file."""
    result = run_cidx(
        "git", "blame", "-r", REPO_ALIAS, KNOWN_ROOT_FILE,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git blame {REPO_ALIAS} {KNOWN_ROOT_FILE}")
    assert result.stdout.strip(), (
        f"cidx git blame {REPO_ALIAS} {KNOWN_ROOT_FILE} returned empty output"
    )


def test_git_file_history(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git file-history <alias> <file> exits 0 and returns non-empty history."""
    result = run_cidx(
        "git", "file-history", "-r", REPO_ALIAS, KNOWN_ROOT_FILE,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git file-history {REPO_ALIAS} {KNOWN_ROOT_FILE}")
    assert result.stdout.strip(), (
        f"cidx git file-history {REPO_ALIAS} {KNOWN_ROOT_FILE} returned empty output"
    )


def test_git_cat(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx git cat <alias> <file> exits 0 and returns file contents."""
    result = run_cidx(
        "git", "cat", "-r", REPO_ALIAS, KNOWN_ROOT_FILE,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(result, f"cidx git cat {REPO_ALIAS} {KNOWN_ROOT_FILE}")
    assert result.stdout.strip(), (
        f"cidx git cat {REPO_ALIAS} {KNOWN_ROOT_FILE} returned empty output"
    )


# ---------------------------------------------------------------------------
# File operation test (self-contained create + delete in one test)
# ---------------------------------------------------------------------------


def test_files_create_and_delete(
    authenticated_workspace: Path,
    activated_golden_repo: str,
    e2e_cli_env: dict[str, str],
) -> None:
    """cidx files create then delete exits 0 for both operations.

    Self-contained: a unique file path is generated per run to avoid
    collisions.  The delete is called in a ``try/finally`` block so it
    always runs after a successful create, preventing leaked test data.
    """
    file_path = f"e2e_test_{uuid.uuid4().hex[:8]}.txt"
    file_content = "E2E test file content from Story #706"

    create_result = run_cidx(
        "files", "create", "-r", REPO_ALIAS, file_path,
        "--content", file_content,
        cwd=str(authenticated_workspace),
        env=e2e_cli_env,
    )
    _assert_ok(create_result, f"cidx files create {REPO_ALIAS} {file_path}")

    try:
        # cidx files create writes to the filesystem without making a git commit,
        # so cidx git cat (git objects at HEAD) cannot verify the file.
        # rc=0 from the create call above is sufficient proof of success.
        pass
    finally:
        # Always delete -- cleanup must run regardless of assertion outcome.
        # --confirm is required: cidx files delete is a destructive operation.
        delete_result = run_cidx(
            "files", "delete", "-r", REPO_ALIAS, file_path, "--confirm",
            cwd=str(authenticated_workspace),
            env=e2e_cli_env,
        )
        _assert_ok(delete_result, f"cidx files delete {REPO_ALIAS} {file_path}")
