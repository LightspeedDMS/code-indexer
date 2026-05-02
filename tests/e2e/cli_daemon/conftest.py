"""
Local conftest for Phase 2: CLI Daemon tests.

Provides session-scoped fixtures that prepare a working copy of markupsafe
with daemon mode enabled.  Phase 2 is self-sufficient: the ``daemon_repo``
fixture does its own init + index from the seed repo so it does not depend
on ``indexed_markupsafe`` from the Phase 1 conftest.

Fixture dependency graph:
  daemon_cli_env (session)
      depends on: e2e_cli_env

  daemon_repo (session)
      depends on: e2e_seed_repo_paths, e2e_cli_env, tmp_path_factory
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.e2e.helpers import run_cidx


@pytest.fixture(scope="session")
def daemon_cli_env(e2e_cli_env: dict[str, str]) -> dict[str, str]:
    """Return ``e2e_cli_env`` unchanged as a named alias for daemon tests.

    Provides a consistent fixture name so daemon test functions declare
    ``daemon_cli_env`` as their env dependency, keeping the intent clear.
    """
    return e2e_cli_env


@pytest.fixture(scope="session")
def daemon_repo(
    e2e_seed_repo_paths,
    e2e_cli_env: dict[str, str],
    tmp_path_factory,
) -> Path:
    """Prepare a fresh markupsafe working copy with daemon mode enabled.

    Phase 2 is self-sufficient: this fixture copies from the seed repo and
    runs init + index itself so it does not depend on ``indexed_markupsafe``
    from the Phase 1 conftest (which is unavailable when Phase 2 runs alone).

    Steps performed once per session:
    1. Copy the seed markupsafe repo into a pytest-managed temp directory.
    2. Run ``cidx init`` to initialise the ``.code-indexer/`` directory.
    3. Run ``cidx index`` to build the vector index.
    4. Run ``cidx config --daemon`` to enable daemon mode in the config.

    Returns the path to the isolated working copy.  The temp directory is
    managed by ``tmp_path_factory`` and cleaned up at session end.
    """
    base = tmp_path_factory.mktemp("cidx_e2e_daemon")
    dest = base / "markupsafe"
    shutil.copytree(str(e2e_seed_repo_paths.markupsafe), str(dest))

    init_result = run_cidx("init", cwd=dest, env=e2e_cli_env)
    assert init_result.returncode == 0, (
        f"daemon_repo: cidx init failed (rc={init_result.returncode})\n"
        f"stdout: {init_result.stdout}\nstderr: {init_result.stderr}"
    )

    index_result = run_cidx("index", cwd=dest, env=e2e_cli_env)
    assert index_result.returncode == 0, (
        f"daemon_repo: cidx index failed (rc={index_result.returncode})\n"
        f"stdout: {index_result.stdout}\nstderr: {index_result.stderr}"
    )

    config_result = run_cidx("config", "--daemon", cwd=dest, env=e2e_cli_env)
    assert config_result.returncode == 0, (
        f"daemon_repo: cidx config --daemon failed "
        f"(rc={config_result.returncode})\n"
        f"stdout: {config_result.stdout}\nstderr: {config_result.stderr}"
    )

    return Path(dest)
