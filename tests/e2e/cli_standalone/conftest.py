"""
Local conftest for Phase 1: CLI Standalone tests.

Provides session-scoped fixtures that set up shared state for AC2 (query)
and AC3 (utilities) tests.  The ``indexed_markupsafe`` fixture runs
``cidx init`` + ``cidx index`` exactly once per session so that query
and utilities tests can reuse the already-indexed working copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


@pytest.fixture(scope="session")
def indexed_markupsafe(
    e2e_seed_repo_paths,
    e2e_cli_env: dict[str, str],
) -> Path:
    """Run ``cidx init`` + ``cidx index`` on markupsafe once per session.

    Returns the markupsafe working-copy path so tests can use it as cwd.
    The index is created in ``.code-indexer/`` inside the working copy.
    This fixture is consumed by query tests (AC2) and utility tests (AC3)
    so they do not need to re-index, keeping the suite fast.
    """
    path = e2e_seed_repo_paths.markupsafe

    # Idempotent: skip init if .code-indexer/ already exists (test_init_seed_repo
    # may have already initialized this working copy).
    if not (path / ".code-indexer").is_dir():
        init_result = run_cidx("init", cwd=path, env=e2e_cli_env)
        assert init_result.returncode == 0, (
            f"indexed_markupsafe: init failed (rc={init_result.returncode})\n"
            f"stdout: {init_result.stdout}\n"
            f"stderr: {init_result.stderr}"
        )

    index_result = run_cidx("index", cwd=path, env=e2e_cli_env)
    assert index_result.returncode == 0, (
        f"indexed_markupsafe: index failed (rc={index_result.returncode})\n"
        f"stdout: {index_result.stdout}\n"
        f"stderr: {index_result.stderr}"
    )

    return path
