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
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

# Generous ceiling for a single-module Python import in a fresh subprocess;
# not a tight performance budget, just a hang guard.
SUBPROCESS_IMPORT_TIMEOUT_SECONDS = 30
THREAD_JOIN_TIMEOUT_SECONDS = 10


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


def _write_stub_config_file(tmp_path: Path) -> Path:
    """Real (empty) config.json so `config_manager.config_path.exists()`
    is True without needing to mock `Path.exists` itself."""
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text("{}")
    return config_path


def _build_stub_config_manager(config_path: Path, codebase_dir: str):
    """A stand-in for ConfigManager.create_with_backtrack()'s return
    value: real enough for _build_vector_store_client's own logic
    (config_path.exists() check + get_config().codebase_dir read)."""

    class StubConfig:
        pass

    StubConfig.codebase_dir = codebase_dir  # type: ignore[attr-defined]

    class StubConfigManager:
        def __init__(self) -> None:
            self.config_path = config_path

        def get_config(self) -> "StubConfig":
            return StubConfig()

    return StubConfigManager()


def _build_reentrant_vector_store_cls(
    service, construction_count: dict, reentrant_outcome: dict
):
    """A stand-in for FilesystemVectorStore whose __init__ fires a
    re-entrant `service.vector_store_client` probe from WITHIN
    construction, on the same thread -- mirrors
    test_file_service_deferred_construction_1650.py's ReentrantARM."""

    class ReentrantFilesystemVectorStore:
        def __init__(self, *args, **kwargs) -> None:
            construction_count["n"] += 1
            if construction_count["n"] == 1:
                try:
                    reentrant_outcome["value"] = service.vector_store_client
                except Exception as e:  # noqa: BLE001 - captured for assertion
                    reentrant_outcome["exception"] = e

    return ReentrantFilesystemVectorStore


def _run_reentrancy_probe(
    stats_service_module, stub_config_manager, reentrant_cls, service
) -> dict:
    """Run the outer `service.vector_store_client` access on a background
    thread, patched so construction triggers the re-entrant probe, and
    join with a bounded timeout so a deadlock fails the test instead of
    hanging it."""
    result: dict = {}

    def worker() -> None:
        try:
            with (
                patch.object(
                    stats_service_module.ConfigManager,
                    "create_with_backtrack",
                    return_value=stub_config_manager,
                ),
                patch.object(
                    stats_service_module, "FilesystemVectorStore", reentrant_cls
                ),
            ):
                result["value"] = service.vector_store_client
        except Exception as e:  # noqa: BLE001 - captured for assertion
            result["exception"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
    result["thread_alive"] = t.is_alive()
    return result


def _assert_reentrancy_outcome(
    construction_count: dict, reentrant_outcome: dict, result: dict
) -> None:
    assert not result["thread_alive"], (
        "REGRESSION: re-entrant access during construction hung "
        "(unbounded recursion or deadlock)."
    )
    assert construction_count["n"] == 1, (
        "BUG #1691 REMEDIATION REGRESSION: FilesystemVectorStore must be "
        "constructed EXACTLY ONCE -- a re-entrant call arriving "
        "mid-construction must not trigger a second/recursive "
        f"construction. construction_count={construction_count['n']}"
    )
    assert "exception" in reentrant_outcome, (
        "The re-entrant call must raise (matching pre-fix unbound "
        f"semantics), not silently return a value. Got: {reentrant_outcome}"
    )
    assert isinstance(reentrant_outcome.get("exception"), RuntimeError), (
        "The re-entrant call's exception must be a RuntimeError (a plain "
        "@property re-entrancy guard, not a module-level __getattr__ "
        "deferral, so AttributeError is deliberately avoided -- see "
        f"vector_store_client's docstring). Got: {reentrant_outcome}"
    )
    assert "value" in result, f"outer call must succeed: {result}"
    assert "exception" not in result, f"outer (original) call must not raise: {result}"


class TestVectorStoreClientReentrancyDoesNotRecurse:
    """Code review gap on the #1691 fix: CLAUDE.md's "Module-Level Service
    Singletons Must Be Lazy (PEP 562)" section mandates that any fix in
    this class MUST include a re-entrancy discriminating test -- none of
    the tests above exercise it. The reviewer independently proved (via a
    manual probe) that the current `threading.RLock` +
    `_vsc_initializing` sentinel implementation is correct, but nothing in
    the committed suite would catch a future regression (e.g. someone
    "simplifying" the RLock to a plain Lock -- which self-deadlocks on
    re-entrant access on the SAME thread -- or removing the
    `_vsc_initializing` sentinel -- which would allow double
    construction).

    Mirrors test_file_service_deferred_construction_1650.py's
    TestActivatedRepoManagerReentrancyDoesNotRecurse exactly: patch the
    dependency inside the construction chain (here, `ConfigManager` and
    `FilesystemVectorStore` as imported into stats_service.py) so that
    `FilesystemVectorStore` construction itself triggers a SECOND access
    to `service.vector_store_client` on the SAME THREAD, mid-construction
    -- then run the outer access from a background thread with a bounded
    `join(timeout=...)`.
    """

    def test_reentrant_access_during_construction_does_not_recurse(
        self, tmp_path, monkeypatch
    ) -> None:
        import code_indexer.server.services.stats_service as stats_service_module
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        # Bug #1690: _build_vector_store_client now also verifies
        # config.codebase_dir strictly equals the REAL process CWD
        # (independent of this test's mocked ConfigManager stub). chdir
        # into tmp_path so that check passes and this orthogonal
        # thread-safety test can focus on re-entrancy, not path matching.
        monkeypatch.chdir(tmp_path)

        config_path = _write_stub_config_file(tmp_path)
        stub_config_manager = _build_stub_config_manager(config_path, str(tmp_path))

        service = RepositoryStatsService()
        construction_count = {"n": 0}
        reentrant_outcome: dict = {}
        reentrant_cls = _build_reentrant_vector_store_cls(
            service, construction_count, reentrant_outcome
        )

        result = _run_reentrancy_probe(
            stats_service_module, stub_config_manager, reentrant_cls, service
        )

        _assert_reentrancy_outcome(construction_count, reentrant_outcome, result)
