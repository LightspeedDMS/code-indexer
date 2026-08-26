"""
Bug #1691: `stats_service.py`'s eager `RepositoryStatsService.__init__`
reproduces the CWD-fallback bug from Bug #1683.

`RepositoryStatsService.__init__` called `ConfigManager.create_with_backtrack()`
with NO starting directory, which backtracks from `Path.cwd()`. When no
`.code-indexer/config.json` is found there (or any parent), that helper
silently falls back to a bare `Config()` with `codebase_dir = Path(".")`,
and the eagerly-constructed `FilesystemVectorStore(base_path=index_dir, ...)`
then creates a stray `.code-indexer/index` directory relative to whatever
the CWD happens to be -- confirmed live on the real dev server, where the
module-level `stats_service = RepositoryStatsService()` singleton runs this
exact path at IMPORT TIME (server process CWD, no real repo context).

Fix: defer `vector_store_client` construction to first real access via a
lazy property (mirrors the Bug #1650 `activated_repo_manager` property
pattern in `file_service.py`/`git_operations_service.py`), and when that
lazy construction does run, verify a REAL config was found
(`config_path.exists()`) before trusting it -- mirroring the Bug #1683
guard already established in `AutoWatchManager.start_watch`.

NOTE on test isolation: this dev machine has real `.code-indexer/config.json`
files at several ancestors of any `/tmp`-based `tmp_path` (e.g.
`/tmp/.code-indexer`, `/home/jsbattig/.code-indexer`) from unrelated real
usage, so an UNMOCKED `ConfigManager.create_with_backtrack()` call starting
from `tmp_path` would accidentally find one of those real configs instead of
hitting the "no config found anywhere" branch this bug is about. Each test
here forces that branch deterministically via the `CODEBASE_DIR` env-var
override that `create_with_backtrack()` already supports in production: it
bypasses backtracking entirely and points straight at
`{CODEBASE_DIR}/.code-indexer/config.json`, which we ensure does not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Generous ceiling for a single-module Python import in a fresh subprocess;
# not a tight performance budget, just a hang guard.
SUBPROCESS_IMPORT_TIMEOUT_SECONDS = 30


@pytest.fixture
def isolated_no_config_cwd(tmp_path, monkeypatch):
    """Chdir into tmp_path and force config discovery to find nothing,
    regardless of real .code-indexer directories elsewhere on this host,
    via the real CODEBASE_DIR override (bypasses ancestor backtracking)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEBASE_DIR", str(tmp_path))
    return tmp_path


class TestRepositoryStatsServiceNoCwdFallbackSideEffect:
    """Bug #1691: construction must not eagerly touch the filesystem."""

    def test_construction_does_not_create_code_indexer_index_at_cwd(
        self, isolated_no_config_cwd
    ) -> None:
        """Constructing RepositoryStatsService() from a CWD with no real
        .code-indexer config must not create a stray .code-indexer/index
        directory relative to that CWD."""
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        RepositoryStatsService()

        assert not (isolated_no_config_cwd / ".code-indexer").exists(), (
            "RepositoryStatsService() must not eagerly create "
            ".code-indexer/index relative to CWD when no real repo "
            "config is found (Bug #1691)"
        )

    def test_vector_store_client_access_raises_loud_when_no_real_config(
        self, isolated_no_config_cwd
    ) -> None:
        """Even when a caller genuinely accesses vector_store_client with
        no real repo config discoverable, the service must fail loud
        rather than silently defaulting to CWD (Bug #1691, mirrors the
        Bug #1683 AutoWatchManager.start_watch config_path.exists() guard)."""
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        service = RepositoryStatsService()

        with pytest.raises(RuntimeError):
            _ = service.vector_store_client

        assert not (isolated_no_config_cwd / ".code-indexer").exists(), (
            "Failed vector_store_client resolution must not leave a stray "
            ".code-indexer/index directory behind (Bug #1691)"
        )

    def test_module_level_singleton_import_does_not_touch_cwd(self, tmp_path) -> None:
        """Bug #1691: importing the module-level `stats_service` singleton
        must not create .code-indexer/index at whatever CWD happens to be
        active at import time.

        Runs in a genuinely separate subprocess (rather than
        importlib.reload() in-process) so this test cannot corrupt this
        pytest session's already-imported stats_service module identity
        for other tests -- e.g. routers/inline_repos_v2.py imports
        `stats_service` at module scope, and reloading stats_service.py
        in-process would rebind RepositoryStatsService to a NEW class
        object that pre-existing instances/patches no longer match.
        """
        src_root = str(Path(__file__).resolve().parents[4] / "src")
        subprocess_env = {
            **os.environ,
            "PYTHONPATH": src_root,
            "CODEBASE_DIR": str(tmp_path),
        }

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import code_indexer.server.services.stats_service",
            ],
            cwd=str(tmp_path),
            env=subprocess_env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_IMPORT_TIMEOUT_SECONDS,
        )

        assert result.returncode == 0, (
            f"Importing stats_service.py failed: {result.stderr}"
        )
        assert not (tmp_path / ".code-indexer").exists(), (
            "Importing stats_service.py must not eagerly create "
            ".code-indexer/index relative to the import-time CWD (Bug #1691)"
        )
