"""Phase-3 auto-watch-disabled regression guard.

Phase 3 is a REST/MCP functional suite; watch-mode behaviour is Phase 2's
concern.  Watch daemons accumulating in the session-scoped Phase-3 server
churn the working tree (repeated CREATED/'File deletion detected' on
.git/index.lock) and are the suspected TOCTOU trigger for
  "no configuration found - project needs initialization"
during golden-repo registration (ms1139scip / Story #1139).

This test suite has two guards:

1. Source-text guard: tests/e2e/server/conftest.py must set
   auto_watch_manager.auto_watch_enabled = False in the test_client_data_dir
   fixture (before create_app() is called) so no watch daemon is ever started
   for any repo that the Phase-3 server activates.

2. Runtime-behaviour guard: AutoWatchManager(auto_watch_enabled=False).start_watch()
   must return {"status": "disabled"} — i.e. the flag is the actual gate honoured
   by the server, not a no-op attribute.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.server.services.auto_watch_manager import AutoWatchManager


_REPO_ROOT = Path(__file__).resolve().parents[4]
_PHASE3_CONFTEST = _REPO_ROOT / "tests" / "e2e" / "server" / "conftest.py"


class TestPhase3AutoWatchDisabledSourceGuard:
    """Source-text guard: conftest.py must set auto_watch_enabled = False."""

    def test_conftest_disables_auto_watch_enabled_flag(self) -> None:
        """tests/e2e/server/conftest.py must set auto_watch_manager.auto_watch_enabled = False.

        The assignment must appear in the test_client_data_dir fixture (before
        create_app() runs) so the module-level singleton is already disabled when
        the Phase-3 in-process server initialises.  Without this, watch daemons
        accumulate across ~280 tests and churn the clone working tree, racing with
        the registration workflow's cidx init / cidx index subprocess pair.
        """
        source = _PHASE3_CONFTEST.read_text()
        assert "auto_watch_manager.auto_watch_enabled = False" in source, (
            "tests/e2e/server/conftest.py must set\n"
            "    auto_watch_manager.auto_watch_enabled = False\n"
            "inside the test_client_data_dir fixture (before create_app() is called).\n"
            "This prevents watch-daemon accumulation in the session-scoped Phase-3 server "
            "which is the suspected TOCTOU trigger for the 'no configuration found' "
            "golden-repo registration failure (ms1139scip)."
        )

    def test_conftest_imports_auto_watch_manager_singleton(self) -> None:
        """conftest.py must import the auto_watch_manager singleton to set the flag on it."""
        source = _PHASE3_CONFTEST.read_text()
        assert (
            "from code_indexer.server.services.auto_watch_manager import auto_watch_manager"
            in source
        ), (
            "tests/e2e/server/conftest.py must import the auto_watch_manager singleton:\n"
            "    from code_indexer.server.services.auto_watch_manager import auto_watch_manager\n"
            "so it can set auto_watch_manager.auto_watch_enabled = False before the app starts."
        )


class TestAutoWatchManagerDisabledFlag:
    """Runtime-behaviour guard: auto_watch_enabled=False is the real gate."""

    def test_start_watch_returns_disabled_when_flag_is_false(self, tmp_path) -> None:
        """AutoWatchManager(auto_watch_enabled=False).start_watch() must return status='disabled'.

        This confirms that setting auto_watch_enabled = False on the module-level
        singleton is the correct and sufficient mechanism to prevent any watch
        daemon from starting — the attribute is not a no-op.
        """
        manager = AutoWatchManager(auto_watch_enabled=False)
        try:
            result = manager.start_watch(str(tmp_path))
            assert result["status"] == "disabled", (
                f"Expected status='disabled' when auto_watch_enabled=False, got: {result!r}"
            )
        finally:
            manager.shutdown()

    def test_is_watching_returns_false_when_flag_is_false(self, tmp_path) -> None:
        """No watch state is ever created when auto_watch_enabled=False."""
        manager = AutoWatchManager(auto_watch_enabled=False)
        try:
            manager.start_watch(str(tmp_path))
            assert manager.is_watching(str(tmp_path)) is False, (
                "is_watching() must be False after start_watch() when disabled"
            )
        finally:
            manager.shutdown()

    def test_flag_set_to_false_after_construction_disables_start_watch(
        self, tmp_path
    ) -> None:
        """Setting auto_watch_enabled = False on an already-constructed instance disables it.

        This mirrors how the Phase-3 conftest disables the module-level singleton:
        the singleton is created at import time (auto_watch_enabled=True by default),
        then the conftest sets .auto_watch_enabled = False before create_app() runs.
        """
        manager = AutoWatchManager(auto_watch_enabled=True)
        try:
            # Simulate the conftest disabling it post-construction.
            manager.auto_watch_enabled = False
            result = manager.start_watch(str(tmp_path))
            assert result["status"] == "disabled", (
                f"Expected status='disabled' after setting auto_watch_enabled=False "
                f"post-construction, got: {result!r}"
            )
            assert manager.is_watching(str(tmp_path)) is False
        finally:
            manager.shutdown()
