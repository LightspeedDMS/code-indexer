"""
Phase 1 — AC3: Utility command tests.

Tests run against the markupsafe working copy using the ``indexed_markupsafe``
session fixture from ``conftest.py``.  Non-destructive tests run first;
destructive tests are named ``test_zz_*`` so pytest's default alphabetical
collection order places them after all other tests in this file.

Flag audit deviations from story spec AC3.2-5:
  Spec mentions --add-language, --remove-language, --exclude-dir, --include-dir
  but ``cidx config`` does NOT implement these flags.  Actual flags are:
    --show, --daemon / --no-daemon, --daemon-ttl, --set-diff-context.
  Tests AC3.2-5 are replaced with equivalent real-flag tests.

Flag audit deviation from AC3.10:
  Spec mentions ``cidx clean-data --force`` but ``cidx clean-data`` has no
  --force flag.  ``cidx clean-data`` without flags clears the current project
  and is the correct destructive call.

Total: 10 test cases.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


# ---------------------------------------------------------------------------
# Local helper
# ---------------------------------------------------------------------------


def assert_cidx_ok(
    result: subprocess.CompletedProcess[str], *, context: str = ""
) -> None:
    """Assert that a cidx subprocess completed with exit code 0."""
    prefix = f"{context}: " if context else ""
    assert result.returncode == 0, (
        f"{prefix}cidx exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC3: Non-destructive utility tests
# ---------------------------------------------------------------------------


def test_status(indexed_markupsafe: Path, e2e_cli_env: dict[str, str]) -> None:
    """cidx status exits 0 and produces output."""
    result = run_cidx("status", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="status")
    assert (result.stdout + result.stderr).strip(), "status produced no output"


def test_config_show(indexed_markupsafe: Path, e2e_cli_env: dict[str, str]) -> None:
    """cidx config --show exits 0 and displays current configuration.

    Spec AC3.2 called for --add-language which does not exist.  --show is the
    real read-only config sub-command that exercises the config code path.
    """
    result = run_cidx("config", "--show", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="config --show")
    assert (result.stdout + result.stderr).strip(), "config --show produced no output"


def test_config_enable_daemon(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx config --daemon exits 0 (enables daemon mode in config).

    Spec AC3.3 called for --remove-language.  --daemon exercises the config
    write code path with a real flag.
    """
    result = run_cidx("config", "--daemon", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="config --daemon")


def test_config_disable_daemon(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx config --no-daemon exits 0 (disables daemon mode in config).

    Spec AC3.4 called for --exclude-dir.  --no-daemon reverts the change made
    by test_config_enable_daemon and exercises the config write path.
    """
    result = run_cidx(
        "config", "--no-daemon", cwd=indexed_markupsafe, env=e2e_cli_env
    )
    assert_cidx_ok(result, context="config --no-daemon")


def test_config_set_diff_context(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx config --set-diff-context 3 exits 0 (persists temporal diff context).

    Spec AC3.5 called for --include-dir.  --set-diff-context exercises the
    config write path for a numeric setting.
    """
    result = run_cidx(
        "config", "--set-diff-context", "3",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="config --set-diff-context")


def test_list_collections(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx list-collections exits 0 and returns non-empty output."""
    result = run_cidx("list-collections", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="list-collections")
    assert (result.stdout + result.stderr).strip(), (
        "list-collections produced no output — expected at least one collection"
    )


def test_health(indexed_markupsafe: Path, e2e_cli_env: dict[str, str]) -> None:
    """cidx health exits 0 (HNSW index is healthy after indexing)."""
    result = run_cidx("health", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="health")


def test_fix_config_dry_run(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx fix-config --dry-run exits 0 (preview fixes without modifying)."""
    result = run_cidx(
        "fix-config", "--dry-run", cwd=indexed_markupsafe, env=e2e_cli_env
    )
    assert_cidx_ok(result, context="fix-config --dry-run")


# ---------------------------------------------------------------------------
# AC3: Destructive utility tests — named test_zz_* to run last
# ---------------------------------------------------------------------------


def test_zz_clean_force(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx clean --force --collection voyage-code-3 exits 0 (clears vectors).

    Destructive: removes indexed vectors from the specified collection.
    Placed last (test_zz_* prefix) so non-destructive utility tests run first.
    Multiple collections may exist from prior rebuild variants, so a specific
    collection is named rather than relying on ambient detection.
    """
    result = run_cidx(
        "clean", "--force", "--collection", "voyage-code-3",
        cwd=indexed_markupsafe, env=e2e_cli_env,
    )
    assert_cidx_ok(result, context="clean --force --collection voyage-code-3")


def test_zz_clean_data(
    indexed_markupsafe: Path, e2e_cli_env: dict[str, str]
) -> None:
    """cidx clean-data exits 0 (clears project data for current project).

    Destructive: removes .code-indexer/index/ for the current project.
    Placed last (test_zz_* prefix) so all other tests run first.

    Note: spec mentioned --force but cidx clean-data has no --force flag.
    The command without flags clears current project data and is safe to run
    in the test working copy.
    """
    result = run_cidx("clean-data", cwd=indexed_markupsafe, env=e2e_cli_env)
    assert_cidx_ok(result, context="clean-data")
