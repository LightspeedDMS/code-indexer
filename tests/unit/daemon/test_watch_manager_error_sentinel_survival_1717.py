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

import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.daemon.watch_manager import DaemonWatchManager, _WatchError

_CONSTRUCTION_DEPENDENCY_TARGETS = [
    "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
    "code_indexer.backends.backend_factory.BackendFactory.create",
    "code_indexer.services.smart_indexer.SmartIndexer",
    "code_indexer.services.git_topology_service.GitTopologyService",
    "code_indexer.services.watch_metadata.WatchMetadata.load_from_disk",
]


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
