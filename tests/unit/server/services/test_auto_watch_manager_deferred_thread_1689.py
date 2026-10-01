"""Bug #1689 remediation: AutoWatchManager.__init__ must be cheap.

See CLAUDE.md's "Module-Level Service Singletons Must Be Lazy (PEP 562)"
section. `AutoWatchManager.__init__` used to start a background
`AutoWatchTimeoutChecker` thread unconditionally, and the module-level
`auto_watch_manager = AutoWatchManager()` statement ran that constructor
at import time -- so importing auto_watch_manager.py spawned a thread as
a side effect. Fix: defer the thread start to first real start_watch()
call; keep the module-level singleton binding eager since construction
itself is now side-effect-free (mirrors file_service.py's Bug #1650 fix).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.config import ConfigManager
from code_indexer.server.services.auto_watch_manager import AutoWatchManager

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 30
THREAD_JOIN_TIMEOUT_SECONDS = 10


class TestConstructionDoesNotStartTimeoutThread:
    """__init__ must not spawn the AutoWatchTimeoutChecker thread as a
    side effect of merely constructing AutoWatchManager."""

    def test_construction_spawns_zero_new_threads(self) -> None:
        # Track actual Thread OBJECTS (not names): the checker thread uses
        # a fixed, non-unique name, so a name-based diff could stay empty
        # even if a NEW thread were spawned, if an old same-named thread
        # already existed. Object identity avoids that false negative.
        before = set(threading.enumerate())
        AutoWatchManager()
        after = set(threading.enumerate())
        new_threads = after - before

        assert new_threads == set(), (
            "BUG #1689 REGRESSION: AutoWatchManager() construction must not "
            f"spawn any background thread. New threads: {new_threads}"
        )

    def test_timeout_thread_attribute_is_none_before_first_start_watch(
        self,
    ) -> None:
        manager = AutoWatchManager()
        assert manager._timeout_thread is None, (
            "AutoWatchManager.__init__ must defer starting the checker "
            "thread until the first real start_watch() call, not spawn it "
            "eagerly in the constructor."
        )


class TestModuleLevelSingletonImportIsThreadFree:
    """The module-level `auto_watch_manager = AutoWatchManager()` singleton
    statement must not spawn a background thread either, since it runs
    unconditionally whenever auto_watch_manager.py is imported
    (transitively via mcp/handlers/files.py, etc).

    Consumer audit (issue #1689): the ONLY production consumers of the
    singleton (mcp/handlers/files.py) do FUNCTION-LOCAL
    `from ...auto_watch_manager import auto_watch_manager` imports inside
    handler functions -- never a module-level from-import -- so there is
    no #1650/#1686-style "from-import defeats laziness" trap to guard
    against here. This test targets the simpler remaining risk: the
    module's OWN `auto_watch_manager = AutoWatchManager()` statement.
    """

    def test_module_import_spawns_zero_checker_threads(self) -> None:
        """Runs in a FRESH SUBPROCESS (mirrors
        test_file_service_deferred_construction_1650.py's established
        pattern) instead of importlib.reload()-ing the real, shared module
        in-process. A fresh subprocess starts with no pre-existing
        AutoWatchTimeoutChecker thread, so a name-based diff is safe here.
        """
        script = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "import threading; "
            "before = {t.name for t in threading.enumerate()}; "
            "import code_indexer.server.services.auto_watch_manager; "
            "after = {t.name for t in threading.enumerate()}; "
            "new_threads = after - before; "
            "watch_threads = [n for n in new_threads if 'autowatch' in n.lower()]; "
            "print('new_autowatch_threads:', watch_threads)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        assert result.returncode == 0, (
            f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "new_autowatch_threads: []" in result.stdout, (
            "BUG #1689 REGRESSION: importing auto_watch_manager.py (running "
            "its module-level `auto_watch_manager = AutoWatchManager()` "
            "statement) spawned the AutoWatchTimeoutChecker thread as an "
            f"import-time side effect. Subprocess output: {result.stdout!r}"
        )


def _write_real_config(repo_path: Path) -> None:
    """Write a real `.code-indexer/config.json` for repo_path.

    `AutoWatchManager.start_watch` fails loud (Bug #1683 round 3/4) when
    no real config is found for repo_path, so the lazy-thread-start tests
    below (which need start_watch to actually reach the
    DaemonWatchManager-construction branch) provide one. The returned
    ConfigManager is stateless after this call and does not need explicit
    cleanup -- create_default_config's return value is intentionally
    unused (it only writes the file; there is nothing to check here).
    """
    config_manager = ConfigManager(repo_path / ".code-indexer" / "config.json")
    config_manager.create_default_config(codebase_dir=repo_path)


class TestFirstStartWatchLazilyStartsCheckerThread:
    """The checker thread must start on first real start_watch() call,
    exactly once, and be a real live daemon thread thereafter."""

    def test_start_watch_starts_checker_thread_exactly_once(self, tmp_path) -> None:
        manager = AutoWatchManager()
        repo_path = str(tmp_path)
        _write_real_config(tmp_path)

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            mock_watch_instance = Mock()
            mock_watch_instance.start_watch.return_value = {"status": "success"}
            mock_daemon.return_value = mock_watch_instance

            assert manager._timeout_thread is None

            manager.start_watch(repo_path, timeout=300)
            first_thread = manager._timeout_thread
            assert first_thread is not None
            assert first_thread.is_alive()
            assert first_thread.name == "AutoWatchTimeoutChecker"

            # Second start_watch (reset-timeout branch) must NOT spawn a
            # second thread.
            manager.start_watch(repo_path, timeout=300)
            assert manager._timeout_thread is first_thread

        manager.shutdown()

    def test_concurrent_start_watch_calls_start_thread_only_once(
        self, tmp_path
    ) -> None:
        """Discriminating boundary test: two threads racing to call
        start_watch() for the first time must only start ONE checker
        thread, not two -- proves the lazy-start guard is actually
        thread-safe, not merely correct in the single-threaded case."""
        manager = AutoWatchManager()
        repo_path_a = str(tmp_path / "repo-a")
        repo_path_b = str(tmp_path / "repo-b")
        Path(repo_path_a).mkdir()
        Path(repo_path_b).mkdir()
        _write_real_config(Path(repo_path_a))
        _write_real_config(Path(repo_path_b))

        before_checker_threads = {
            t for t in threading.enumerate() if t.name == "AutoWatchTimeoutChecker"
        }

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            mock_watch_instance = Mock()
            mock_watch_instance.start_watch.return_value = {"status": "success"}
            mock_daemon.return_value = mock_watch_instance

            barrier = threading.Barrier(2, timeout=THREAD_JOIN_TIMEOUT_SECONDS)

            def worker(repo_path: str) -> None:
                barrier.wait()
                manager.start_watch(repo_path, timeout=300)

            t1 = threading.Thread(target=worker, args=(repo_path_a,))
            t2 = threading.Thread(target=worker, args=(repo_path_b,))
            t1.start()
            t2.start()
            t1.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)
            t2.join(timeout=THREAD_JOIN_TIMEOUT_SECONDS)

            assert not t1.is_alive() and not t2.is_alive()

            after_checker_threads = {
                t for t in threading.enumerate() if t.name == "AutoWatchTimeoutChecker"
            }
            new_checker_threads = after_checker_threads - before_checker_threads
            assert len(new_checker_threads) == 1, (
                "BUG #1689 REGRESSION: concurrent first-time start_watch() "
                "calls must lazily start exactly ONE NEW checker thread, "
                f"got {len(new_checker_threads)}"
            )

        manager.shutdown()


class TestShutdownToleratesNeverStartedThread:
    """shutdown() must not raise when the checker thread was never
    started (e.g. auto_watch_enabled=False and start_watch() was never
    called, or start_watch() was never invoked at all)."""

    def test_shutdown_without_any_start_watch_call_does_not_raise(self) -> None:
        manager = AutoWatchManager()
        manager.shutdown()  # must not raise AttributeError on None thread

    def test_shutdown_when_disabled_does_not_raise(self, tmp_path) -> None:
        manager = AutoWatchManager(auto_watch_enabled=False)
        manager.start_watch(str(tmp_path))
        assert manager._timeout_thread is None
        manager.shutdown()  # must not raise
