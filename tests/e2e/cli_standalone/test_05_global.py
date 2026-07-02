"""
Phase 1 — AC1: Global repository management tests.

Tests run against temporary golden-repos directory structures populated with
copies of the indexed markupsafe working copy.  The ``CIDX_GOLDEN_REPOS_DIR``
environment variable is set per-fixture so all global commands operate in
isolation from the developer's real global config.

Flag audit notes:
  ``cidx global activate REPO_NAME`` — REPO_NAME must exist at
    ``$CIDX_GOLDEN_REPOS_DIR/repos/<REPO_NAME>`` with a
    ``.code-indexer/index`` sub-directory.
  ``cidx global status ALIAS_NAME`` — ALIAS_NAME is the alias created by
    activate (i.e. ``<REPO_NAME>-global``).
  ``cidx global regex-search REPO_IDENTIFIER PATTERN`` — both positional args
    required; REPO_IDENTIFIER is the global alias.
  ``cidx show-global`` — no args, reads ``CIDX_GOLDEN_REPOS_DIR`` (or default).
  ``cidx set-global-refresh INTERVAL`` — positional integer arg, minimum 60.

Storage note:
  ``cidx global activate`` uses ``get_server_global_registry()`` which writes
  to SQLite (``cidx_server.db``).  ``cidx global list``, ``global status``, and
  ``global regex-search`` use ``GlobalRegistry(golden_repos_dir)`` in JSON mode
  (``global_registry.json``).  The ``activated_global_env`` fixture bridges this
  mismatch by registering the alias in the JSON registry after activation.

Fixture design:
  ``_setup_golden_repos_dir`` — module-level helper (not a fixture) that
    creates the temp dir structure and pre-creates ``cidx_server.db``.  Called
    by both session fixtures so the logic is not duplicated.
  ``golden_repos_env`` — session fixture backed by its own temp dir; no
    pre-activation.  Used by tests that test the activate command directly or
    that do not require a pre-activated repo.
  ``activated_global_env`` — session fixture backed by its OWN separate temp
    dir (not shared with ``golden_repos_env``), runs activation, registers in
    JSON registry, and returns a fresh ``dict`` copy of the env.  Used by tests
    that require ``markupsafe-global`` to already exist.

Total: 6 test cases.
"""

from __future__ import annotations

import json as json_module
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.helpers import run_cidx

# DDL for the global_repos table required by GlobalReposSqliteBackend.
# cidx global activate uses get_server_global_registry() which derives
# db_path as: Path(golden_repos_dir).parent / "cidx_server.db"
_GLOBAL_REPOS_DDL = """
CREATE TABLE IF NOT EXISTS global_repos (
    alias_name       TEXT PRIMARY KEY NOT NULL,
    repo_name        TEXT NOT NULL,
    repo_url         TEXT NOT NULL,
    index_path       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    last_refresh     TEXT NOT NULL,
    enable_temporal  INTEGER NOT NULL DEFAULT 0,
    temporal_options TEXT,
    enable_scip      INTEGER NOT NULL DEFAULT 0
)
"""


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def _setup_golden_repos_dir(
    indexed_markupsafe: Path,
    base: Path,
) -> Path:
    """Create golden-repos directory structure and pre-create cidx_server.db.

    Creates:
        <base>/
          cidx_server.db         <- SQLite DB with global_repos table
          golden-repos/
            repos/
              markupsafe/        <- copy of indexed markupsafe working copy
                .code-indexer/
                  index/         <- required by cidx global activate

    Returns the ``golden-repos/`` path.
    """
    golden_repos_dir = base / "golden-repos"
    repos_dir = golden_repos_dir / "repos"
    repos_dir.mkdir(parents=True)

    dest = repos_dir / "markupsafe"
    shutil.copytree(str(indexed_markupsafe), str(dest))

    index_dir = dest / ".code-indexer" / "index"
    index_dir.mkdir(parents=True, exist_ok=True)

    db_path = base / "cidx_server.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(_GLOBAL_REPOS_DDL)

    return golden_repos_dir


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
# Session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def golden_repos_env(
    indexed_markupsafe: Path,
    e2e_cli_env: dict[str, str],
    tmp_path_factory,
) -> dict[str, str]:
    """Build a temp golden-repos directory without pre-activating any repo.

    Used by tests that exercise ``cidx global activate`` directly, and by
    tests that do not require a pre-existing global alias (``show-global``,
    ``set-global-refresh``).

    Returns a fresh env dict extending ``e2e_cli_env`` with its own
    ``CIDX_GOLDEN_REPOS_DIR``.
    """
    base = tmp_path_factory.mktemp("cidx_e2e_global_plain")
    golden_repos_dir = _setup_golden_repos_dir(indexed_markupsafe, base)
    env = dict(e2e_cli_env)
    env["CIDX_GOLDEN_REPOS_DIR"] = str(golden_repos_dir)
    return env


@pytest.fixture(scope="session")
def activated_global_env(
    indexed_markupsafe: Path,
    e2e_cli_env: dict[str, str],
    tmp_path_factory,
) -> dict[str, str]:
    """Build an isolated temp dir, activate markupsafe, and register in JSON.

    Uses its own separate temp directory (not shared with ``golden_repos_env``)
    so there is no mutation dependency between this fixture and
    ``test_global_activate_markupsafe``.

    Steps:
    1. Create fresh golden-repos dir + ``cidx_server.db`` via ``_setup_golden_repos_dir``.
    2. Run ``cidx global activate markupsafe`` (creates alias file + SQLite write).
    3. Read the generated alias JSON file to get the actual ``target_path``.
    4. Register alias in the JSON registry (``use_sqlite=False``) using ``target_path``
       from the alias file so that ``cidx global list / status / regex-search``
       (which read from JSON) find the repo.

    Returns a fresh ``dict`` copy of the env with its own ``CIDX_GOLDEN_REPOS_DIR``.
    """
    base = tmp_path_factory.mktemp("cidx_e2e_global_activated")
    golden_repos_dir = _setup_golden_repos_dir(indexed_markupsafe, base)
    env: dict[str, str] = dict(e2e_cli_env)
    env["CIDX_GOLDEN_REPOS_DIR"] = str(golden_repos_dir)

    # Step 2: activate via CLI (SQLite write + alias file creation)
    result = run_cidx("global", "activate", "markupsafe", env=env)
    assert result.returncode == 0, (
        f"activated_global_env: cidx global activate markupsafe failed "
        f"(rc={result.returncode})\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Step 3: read alias file to get actual target_path
    alias_file = golden_repos_dir / "aliases" / "markupsafe-global.json"
    assert alias_file.exists(), (
        f"activated_global_env: alias file not created at {alias_file}"
    )
    alias_data: dict[str, Any] = json_module.loads(alias_file.read_text())
    target_path = alias_data.get("target_path", "")
    assert target_path, (
        f"activated_global_env: target_path missing from alias file: {alias_data}"
    )

    # Step 4: register in JSON registry so JSON-mode CLI commands find the repo
    from code_indexer.global_repos.global_registry import GlobalRegistry

    registry = GlobalRegistry(str(golden_repos_dir), use_sqlite=False)
    registry.register_global_repo(
        repo_name="markupsafe",
        alias_name="markupsafe-global",
        repo_url=f"local://{golden_repos_dir / 'repos' / 'markupsafe'}",
        index_path=target_path,
    )

    return env


# ---------------------------------------------------------------------------
# AC1: Global repository management tests
# ---------------------------------------------------------------------------


def test_global_activate_markupsafe(
    golden_repos_env: dict[str, str],
) -> None:
    """cidx global activate markupsafe exits 0.

    Uses ``golden_repos_env`` — a fresh dir with no prior activation — so
    this test exercises the activate command on a clean slate.
    """
    result = run_cidx(
        "global",
        "activate",
        "markupsafe",
        env=golden_repos_env,
    )
    assert_cidx_ok(result, context="global activate markupsafe")


def test_global_list(
    activated_global_env: dict[str, str],
) -> None:
    """cidx global list exits 0 and returns non-empty output after activation."""
    result = run_cidx(
        "global",
        "list",
        env=activated_global_env,
    )
    assert_cidx_ok(result, context="global list")
    assert (result.stdout + result.stderr).strip(), "global list produced no output"


def test_global_status_markupsafe(
    activated_global_env: dict[str, str],
) -> None:
    """cidx global status markupsafe-global exits 0 and shows metadata."""
    result = run_cidx(
        "global",
        "status",
        "markupsafe-global",
        env=activated_global_env,
    )
    assert_cidx_ok(result, context="global status markupsafe-global")


def test_global_regex_search_escape(
    activated_global_env: dict[str, str],
) -> None:
    """cidx global regex-search markupsafe-global 'escape' exits 0."""
    result = run_cidx(
        "global",
        "regex-search",
        "markupsafe-global",
        "escape",
        env=activated_global_env,
    )
    assert_cidx_ok(result, context="global regex-search escape")


def test_show_global(
    golden_repos_env: dict[str, str],
) -> None:
    """cidx show-global exits 0 and returns configuration output."""
    result = run_cidx(
        "show-global",
        env=golden_repos_env,
    )
    assert_cidx_ok(result, context="show-global")
    assert (result.stdout + result.stderr).strip(), "show-global produced no output"


def test_set_global_refresh_120(
    golden_repos_env: dict[str, str],
) -> None:
    """cidx set-global-refresh 120 exits 0 and updates refresh interval."""
    result = run_cidx(
        "set-global-refresh",
        "120",
        env=golden_repos_env,
    )
    assert_cidx_ok(result, context="set-global-refresh 120")
