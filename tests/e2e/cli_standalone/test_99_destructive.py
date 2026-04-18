"""
Phase 1 — Destructive CLI commands (runs LAST in Phase 1).

These tests exercise destructive CLI operations that must run after all
other Phase 1 tests have completed.  File prefix ``test_99_`` and test
prefix ``test_zzz_`` together guarantee alphabetical ordering places every
test here after all preceding Phase 1 test files.

Destructive operations covered:
  1. ``cidx clean --force --collection voyage-code-3`` on type-fest.
  2. ``cidx clean-data --all-projects`` to wipe all project index data.
  3. ``cidx uninstall --confirm`` on markupsafe (removes .code-indexer/
     from that copy only — does NOT pass --wipe-all so the global CIDX
     install is preserved).

Flag audit notes:
  - ``cidx clean-data`` has no --force flag; --all-projects is the correct
    flag for wiping all project data.
  - ``cidx uninstall`` has no --dry-run flag; --confirm skips the
    interactive confirmation prompt.  Local-mode uninstall (no --wipe-all)
    removes only the project's .code-indexer/ directory.

Total: 3 test cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


# ---------------------------------------------------------------------------
# Local assertion helper
# ---------------------------------------------------------------------------


def _assert_cidx_ok(
    result: subprocess.CompletedProcess[str], *, context: str = ""
) -> None:
    """Assert that a cidx subprocess completed with exit code 0."""
    prefix = f"{context}: " if context else ""
    assert result.returncode == 0, (
        f"{prefix}cidx exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# Destructive tests — prefixed test_zzz_* to run absolutely last
# ---------------------------------------------------------------------------


def test_zzz_clean_force_on_type_fest(
    e2e_seed_repo_paths, e2e_cli_env: dict[str, str]
) -> None:
    """cidx clean --force --collection voyage-code-3 on type-fest exits 0.

    Destructive: clears indexed vectors from the voyage-code-3 collection
    inside the type-fest working copy.  Uses --force to bypass the
    interactive confirmation prompt.  type-fest is used so this does not
    interfere with markupsafe-based tests which run earlier in the suite.

    Runs last via test_zzz_ prefix after all non-destructive Phase 1 tests.
    """
    type_fest_path: Path = e2e_seed_repo_paths.type_fest
    result = run_cidx(
        "clean", "--force", "--collection", "voyage-code-3",
        cwd=type_fest_path, env=e2e_cli_env,
    )
    _assert_cidx_ok(
        result,
        context="clean --force --collection voyage-code-3 (type-fest)",
    )


def test_zzz_clean_data_all_projects(
    e2e_seed_repo_paths, e2e_cli_env: dict[str, str]
) -> None:
    """cidx clean-data --all-projects exits 0 (wipes index data for all projects).

    Destructive: removes .code-indexer/index/ for every known project.
    Runs from the type-fest working copy directory; --all-projects makes
    the cwd irrelevant for scope but a valid directory avoids path errors.

    Flag audit: cidx clean-data has no --force flag.  --all-projects is
    the correct flag to wipe all project data.

    Runs last via test_zzz_ prefix.
    """
    type_fest_path: Path = e2e_seed_repo_paths.type_fest
    result = run_cidx(
        "clean-data", "--all-projects",
        cwd=type_fest_path, env=e2e_cli_env,
    )
    _assert_cidx_ok(result, context="clean-data --all-projects")


def test_zzz_uninstall_local_confirm(
    e2e_seed_repo_paths, e2e_cli_env: dict[str, str]
) -> None:
    """cidx uninstall --confirm on markupsafe exits 0 (local-mode project cleanup).

    Destructive (project-scoped): removes .code-indexer/ from the markupsafe
    working copy.  Does NOT pass --wipe-all, so the global CIDX install
    (binaries, models, global config) is preserved.  This exercises the
    local-mode uninstall code path without destroying the test environment.

    Flag audit: cidx uninstall has no --dry-run flag.  --confirm skips the
    interactive confirmation prompt so the command runs non-interactively.

    Runs last via test_zzz_ prefix.
    """
    markupsafe_path: Path = e2e_seed_repo_paths.markupsafe
    result = run_cidx(
        "uninstall", "--confirm",
        cwd=markupsafe_path, env=e2e_cli_env,
    )
    _assert_cidx_ok(result, context="uninstall --confirm (markupsafe)")
