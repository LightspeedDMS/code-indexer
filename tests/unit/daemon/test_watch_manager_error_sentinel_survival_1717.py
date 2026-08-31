"""Bug #1717: `_watch_thread_worker`'s `finally` block unconditionally wipes
`self.watch_handler`, overwriting the `_WatchError` sentinel the `except`
block just set on a construction failure -- before any consumer (notably
`get_stats()`) can observe it.

Concrete failure scenario: `_create_watch_handler` raises inside
`_watch_thread_worker`'s try block (e.g. Bug #1713's config-verification
failure, exercised here with a genuinely un-configured project directory --
no mock of `_create_watch_handler` itself). The `except` branch correctly
stores `self.watch_handler = _WatchError(str(e))`, but the `finally` block
immediately below unconditionally executes `self.watch_handler = None`,
erasing the sentinel. `get_stats()` then reports `{"status": "idle"}`
instead of `{"status": "error", "error": ...}}`, silently swallowing the
failure from the caller's perspective.

Test isolation mirrors `test_watch_manager_config_verification_1713.py`: an
`isolated_tmp_root` fixture rooted under `~/.tmp` (never bare `/tmp`) plus
`CODEBASE_DIR` env override so `create_with_backtrack()` cannot silently
find a real ancestor `.code-indexer/config.json` on this dev machine.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.daemon.service import CIDXDaemonService
from code_indexer.daemon.watch_manager import DaemonWatchManager, _WatchError

_CONSTRUCTION_DEPENDENCY_TARGETS = [
    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
    "code_indexer.backends.backend_factory.BackendFactory.create",
    "code_indexer.services.smart_indexer.SmartIndexer",
    "code_indexer.services.git_topology_service.GitTopologyService",
    "code_indexer.services.watch_metadata.WatchMetadata.load_from_disk",
]

# Bounded joins for background threads spawned by the real start_watch()
# entry point exercised in TestExposedWatchStatusRealEntryPoint -- the
# construction failure happens fast (no real I/O), so these are generous
# upper bounds, not expected durations.
_WATCH_THREAD_JOIN_TIMEOUT_SECONDS = 10
_EVICTION_THREAD_JOIN_TIMEOUT_SECONDS = 1


@pytest.fixture
def isolated_tmp_root():
    """A temp directory tree rooted under `~/.tmp` (never `/tmp` -- project
    convention), immune to any real `.code-indexer/config.json` that may
    exist as an ancestor of the system `/tmp`."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1717_"))
    yield root
    shutil.rmtree(root)


@pytest.fixture
def failed_manager(isolated_tmp_root, monkeypatch) -> DaemonWatchManager:
    """A `DaemonWatchManager` that has already run `_watch_thread_worker`
    against a project directory with NO `.code-indexer/config.json`
    findable anywhere (real failure, no SUT method mocked -- the same
    genuine `ConfigVerificationError` repro as Bug #1713)."""
    project_path = isolated_tmp_root / "never-configured-repo"
    project_path.mkdir()
    monkeypatch.setenv("CODEBASE_DIR", str(project_path))

    manager = DaemonWatchManager()
    manager._watch_thread_worker(str(project_path), config=None)
    return manager


class TestWatchThreadWorkerPreservesErrorSentinel:
    """A genuine construction failure must leave `self.watch_handler` as a
    `_WatchError` instance after `_watch_thread_worker` returns -- the
    `finally` block must not clobber it."""

    def test_watch_handler_remains_watch_error_after_construction_failure(
        self, failed_manager
    ) -> None:
        assert isinstance(failed_manager.watch_handler, _WatchError), (
            "finally block must not clobber the _WatchError sentinel set "
            f"by the except block; got {failed_manager.watch_handler!r} instead"
        )

    def test_get_stats_reports_error_status_after_construction_failure(
        self, failed_manager
    ) -> None:
        stats = failed_manager.get_stats()

        assert stats["status"] == "error", (
            "get_stats() must surface the construction failure as an "
            f"error status, not silently report idle; got {stats!r}"
        )
        assert stats["error"]


class TestWatchThreadWorkerNormalExitStillClearsState:
    """Regression guard: a NORMAL (non-error) thread exit must still reset
    watch_handler/project_path/start_time to idle, exactly as before this
    fix -- only the error path is special-cased. Every heavy dependency
    `_create_watch_handler` constructs is a genuine external collaborator
    (embedding provider, vector store backend, indexer, git topology,
    watch metadata, the filesystem watch handler itself), each patched
    individually -- `_create_watch_handler` and `_watch_thread_worker`
    themselves run unmodified."""

    def test_normal_exit_resets_to_idle(self, isolated_tmp_root) -> None:
        project_path = isolated_tmp_root / "real-repo"
        project_path.mkdir()
        ConfigManager(
            project_path / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=project_path)

        fake_handler = MagicMock()
        fake_handler.is_watching.return_value = False

        manager = DaemonWatchManager()
        # Pre-set the stop event so the worker's wait-loop breaks on its
        # first iteration instead of blocking for the real 1s timeout.
        manager._stop_event.set()

        with ExitStack() as stack:
            for target in _CONSTRUCTION_DEPENDENCY_TARGETS:
                stack.enter_context(patch(target, return_value=MagicMock()))
            stack.enter_context(
                patch(
                    "code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler",
                    return_value=fake_handler,
                )
            )

            manager._watch_thread_worker(str(project_path), config=None)

        assert manager.watch_handler is None
        assert manager.project_path is None
        assert manager.start_time is None

        stats = manager.get_stats()
        assert stats["status"] == "idle"


class TestStopWatchAfterFailedConstructionSentinel:
    """Code-review Finding 2 on #1717: making `_WatchError` a PERSISTENT
    value of `self.watch_handler` (instead of getting wiped) means it now
    reaches `stop_watch()`'s pre-existing guards, which previously only
    knew about `_WatchStarting`. Pre-fix, `_WatchError` has no
    `stop_watching()` method, so `stop_watch()` raises AttributeError
    (swallowed by a broad `except Exception`) and logs a spurious ERROR,
    then reports the misleading `{"status": "success", ...}` instead of
    correctly reporting that nothing was running."""

    def test_stop_watch_returns_error_status_not_misleading_success(
        self, failed_manager
    ) -> None:
        result = failed_manager.stop_watch()

        assert result["status"] == "error", (
            "stop_watch() after a failed construction must report a "
            f"deliberate error status, not a misleading success; got {result!r}"
        )

    def test_stop_watch_logs_no_spurious_error_about_missing_stop_watching(
        self, failed_manager, caplog
    ) -> None:
        with caplog.at_level(logging.ERROR, logger="code_indexer.daemon.watch_manager"):
            failed_manager.stop_watch()

        spurious = [
            record.getMessage()
            for record in caplog.records
            if "Error stopping watch handler" in record.getMessage()
        ]
        assert not spurious, (
            "stop_watch() must not attempt to call stop_watching() on a "
            f"_WatchError sentinel (no such method exists); got: {spurious}"
        )


class TestExposedWatchStatusRealEntryPoint:
    """Code-review Findings 1 and 3 on #1717.

    Finding 1: the issue explicitly named `exposed_watch_status`/
    `get_stats()` TOGETHER as needing to observe the surviving sentinel --
    fixing only `DaemonWatchManager.get_stats()` leaves the RPC-facing
    `CIDXDaemonService.exposed_watch_status()` collapsing the new "error"
    status into the same not-running payload as genuine idle, so nothing
    observable changes for the RPC client / CLI user.

    Finding 3: the prior tests drove `_watch_thread_worker` directly,
    bypassing `start_watch()` -- the ONLY place `self.project_path` is
    actually assigned -- leaving zero coverage of `project_path`
    preservation on the error path. This test drives the REAL
    `start_watch()` entry point (background thread spawn + join) end to
    end through the real `CIDXDaemonService.exposed_watch_start()` RPC
    method, exactly as a real daemon client would."""

    def test_exposed_watch_status_reports_error_after_real_start_watch_failure(
        self, isolated_tmp_root, monkeypatch
    ) -> None:
        project_path = isolated_tmp_root / "never-configured-repo"
        project_path.mkdir()
        monkeypatch.setenv("CODEBASE_DIR", str(project_path))

        service = CIDXDaemonService()
        try:
            start_result = service.exposed_watch_start(str(project_path))
            # Story #472: the RPC ack is always immediate/non-blocking --
            # this is the exact "told successfully started" behavior the
            # issue describes; the failure only becomes visible later.
            assert start_result["status"] == "success"

            thread = service.watch_manager.watch_thread
            assert thread is not None
            thread.join(timeout=_WATCH_THREAD_JOIN_TIMEOUT_SECONDS)
            assert not thread.is_alive(), "watch thread did not exit in time"

            manager_stats = service.watch_manager.get_stats()
            assert manager_stats["status"] == "error"
            assert manager_stats["project_path"] == str(project_path), (
                "project_path must be preserved on the error path -- it is "
                "only ever assigned by the real start_watch() entry point"
            )

            status = service.exposed_watch_status()
            assert status["running"] is False
            assert status.get("project_path") == str(project_path), (
                "exposed_watch_status() must surface the failed watch's "
                f"project_path, not collapse it to None; got {status!r}"
            )
            assert status.get("error"), (
                "exposed_watch_status() must surface the construction "
                f"failure's error message; got {status!r}"
            )
        finally:
            service.eviction_thread.stop()
            service.eviction_thread.join(timeout=_EVICTION_THREAD_JOIN_TIMEOUT_SECONDS)
