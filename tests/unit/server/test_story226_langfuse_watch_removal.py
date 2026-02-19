"""
Story #226: Refactor Langfuse repos to versioned golden repo platform (eliminate watch-mode).

These tests verify that the watch-mode indexing code has been removed:
- No subprocess.run() calls in register_langfuse_golden_repos()
- No auto_watch_manager.start_watch() calls in _on_langfuse_sync_complete()
- DaemonWatchManager has no _create_simple_watch_handler method
- langfuse_watch_integration module is deleted
- watch_manager.py has no langfuse_watch_integration import
- _create_watch_handler() has no non-git/SimpleWatchHandler branch
"""

import inspect
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRegisterLangfuseGoldenReposNoSubprocess:
    """C1: Verify register_langfuse_golden_repos() does not call subprocess.run()."""

    def test_register_langfuse_golden_repos_no_subprocess(self, tmp_path):
        """register_langfuse_golden_repos() calls register_local_repo() but never subprocess.run().

        After the removal, in-place cidx init/index calls are gone.
        RefreshScheduler handles indexing instead.
        """
        # Create a langfuse_ folder structure in tmp_path
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()
        langfuse_folder = golden_repos_dir / "langfuse_project_user"
        langfuse_folder.mkdir()

        # Create a mock GoldenRepoManager
        mock_manager = MagicMock()
        mock_manager.register_local_repo.return_value = True  # newly_registered

        # Import the function under test
        from code_indexer.server.app import register_langfuse_golden_repos

        # Patch subprocess.run to detect any forbidden calls
        with patch("subprocess.run") as mock_subprocess_run:
            register_langfuse_golden_repos(mock_manager, str(golden_repos_dir))

            # subprocess.run must NEVER be called
            mock_subprocess_run.assert_not_called()

        # But register_local_repo() MUST have been called
        mock_manager.register_local_repo.assert_called_once()

    def test_register_langfuse_golden_repos_calls_register_local_repo_for_each_folder(
        self, tmp_path
    ):
        """register_langfuse_golden_repos() calls register_local_repo() for each langfuse_ folder."""
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()

        # Create multiple langfuse_ folders
        for name in ["langfuse_proj1", "langfuse_proj2", "langfuse_proj3"]:
            (golden_repos_dir / name).mkdir()

        # Create a non-langfuse folder (should be ignored)
        (golden_repos_dir / "not_langfuse_folder").mkdir()

        mock_manager = MagicMock()
        mock_manager.register_local_repo.return_value = False  # already registered

        from code_indexer.server.app import register_langfuse_golden_repos

        with patch("subprocess.run") as mock_subprocess_run:
            register_langfuse_golden_repos(mock_manager, str(golden_repos_dir))
            mock_subprocess_run.assert_not_called()

        # Exactly 3 calls — one per langfuse_ folder, non-langfuse folder ignored
        assert mock_manager.register_local_repo.call_count == 3

    def test_register_langfuse_golden_repos_nonexistent_dir_is_noop(self):
        """register_langfuse_golden_repos() does nothing if golden_repos_dir does not exist."""
        mock_manager = MagicMock()

        from code_indexer.server.app import register_langfuse_golden_repos

        # Should not raise, not call subprocess, not call register_local_repo
        with patch("subprocess.run") as mock_subprocess_run:
            register_langfuse_golden_repos(mock_manager, "/nonexistent/path/golden-repos")
            mock_subprocess_run.assert_not_called()

        mock_manager.register_local_repo.assert_not_called()


class TestOnLangfuseSyncCompleteNoWatchStart:
    """C2: Verify _on_langfuse_sync_complete() does not start auto_watch_manager watches."""

    def test_on_langfuse_sync_complete_no_watch_start(self):
        """_on_langfuse_sync_complete() must not reference auto_watch_manager or start_watch.

        Since _on_langfuse_sync_complete is a nested closure inside create_app(),
        we verify it by inspecting the app.py source code directly.
        """
        app_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "app.py"
        )
        source = app_path.read_text()

        assert "def _on_langfuse_sync_complete" in source, (
            "_on_langfuse_sync_complete must exist in app.py"
        )

        # Extract the function body by tracking indentation
        lines = source.splitlines()
        in_function = False
        function_lines = []
        base_indent = None

        for line in lines:
            if "def _on_langfuse_sync_complete" in line:
                in_function = True
                base_indent = len(line) - len(line.lstrip())
                function_lines.append(line)
                continue
            if in_function:
                if line.strip() == "":
                    function_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= base_indent and line.strip():
                    break
                function_lines.append(line)

        function_source = "\n".join(function_lines)

        assert "auto_watch_manager" not in function_source, (
            "_on_langfuse_sync_complete must NOT reference auto_watch_manager (Story #226). "
            f"Function body:\n{function_source}"
        )
        assert "start_watch" not in function_source, (
            "_on_langfuse_sync_complete must NOT call start_watch() (Story #226). "
            f"Function body:\n{function_source}"
        )
        assert "reset_timeout" not in function_source, (
            "_on_langfuse_sync_complete must NOT call reset_timeout() (Story #226). "
            f"Function body:\n{function_source}"
        )


class TestDaemonWatchManagerNoSimpleWatchHandler:
    """C5/C6: Verify DaemonWatchManager has no _create_simple_watch_handler method."""

    def test_daemon_watch_manager_no_simple_watch_handler(self):
        """DaemonWatchManager must not have _create_simple_watch_handler attribute."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        assert not hasattr(DaemonWatchManager, "_create_simple_watch_handler"), (
            "DaemonWatchManager._create_simple_watch_handler must be removed (Story #226)"
        )

    def test_daemon_watch_manager_no_is_git_folder(self):
        """DaemonWatchManager must not have _is_git_folder method (removed with simple watch handler)."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        assert not hasattr(DaemonWatchManager, "_is_git_folder"), (
            "DaemonWatchManager._is_git_folder must be removed (Story #226)"
        )

    def test_daemon_watch_manager_still_has_create_watch_handler(self):
        """DaemonWatchManager._create_watch_handler must still exist after cleanup."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        assert hasattr(DaemonWatchManager, "_create_watch_handler"), (
            "DaemonWatchManager._create_watch_handler must remain after Story #226 cleanup"
        )


class TestLangfuseWatchIntegrationModuleDeleted:
    """C3: Verify langfuse_watch_integration module is deleted."""

    def test_langfuse_watch_integration_module_deleted(self):
        """Importing langfuse_watch_integration must raise ImportError."""
        module_name = "code_indexer.server.services.langfuse_watch_integration"
        if module_name in sys.modules:
            del sys.modules[module_name]

        with pytest.raises(ImportError):
            import code_indexer.server.services.langfuse_watch_integration  # noqa: F401

    def test_langfuse_watch_integration_class_not_importable(self):
        """Importing LangfuseWatchIntegration class must raise ImportError."""
        module_name = "code_indexer.server.services.langfuse_watch_integration"
        if module_name in sys.modules:
            del sys.modules[module_name]

        with pytest.raises(ImportError):
            from code_indexer.server.services.langfuse_watch_integration import (  # noqa: F401
                LangfuseWatchIntegration,
            )


class TestWatchManagerNoLangfuseImports:
    """C4: Verify watch_manager.py has no langfuse_watch_integration imports."""

    def test_watch_manager_no_langfuse_imports(self):
        """watch_manager.py source must not contain langfuse_watch_integration import."""
        watch_manager_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "daemon"
            / "watch_manager.py"
        )

        source = watch_manager_path.read_text()

        assert "langfuse_watch_integration" not in source, (
            "watch_manager.py must not import from langfuse_watch_integration (Story #226)"
        )

    def test_watch_manager_no_default_langfuse_timeout_constant(self):
        """watch_manager.py must not reference DEFAULT_LANGFUSE_WATCH_IDLE_TIMEOUT_SECONDS."""
        watch_manager_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "daemon"
            / "watch_manager.py"
        )

        source = watch_manager_path.read_text()

        assert "DEFAULT_LANGFUSE_WATCH_IDLE_TIMEOUT_SECONDS" not in source, (
            "watch_manager.py must not reference DEFAULT_LANGFUSE_WATCH_IDLE_TIMEOUT_SECONDS (Story #226)"
        )


class TestCreateWatchHandlerNoNonGitBranch:
    """C6: Verify _create_watch_handler() has no SimpleWatchHandler branch."""

    def test_create_watch_handler_no_simple_watch_handler_reference(self):
        """_create_watch_handler() source must not reference SimpleWatchHandler."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        source = inspect.getsource(DaemonWatchManager._create_watch_handler)

        assert "SimpleWatchHandler" not in source, (
            "_create_watch_handler must not reference SimpleWatchHandler (Story #226)"
        )

    def test_create_watch_handler_no_simple_watch_handler_call(self):
        """_create_watch_handler() must not call _create_simple_watch_handler."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        source = inspect.getsource(DaemonWatchManager._create_watch_handler)

        assert "_create_simple_watch_handler" not in source, (
            "_create_watch_handler must not call _create_simple_watch_handler (Story #226)"
        )

    def test_create_watch_handler_no_is_git_folder_call(self):
        """_create_watch_handler() must not call _is_git_folder (no branching on folder type)."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        source = inspect.getsource(DaemonWatchManager._create_watch_handler)

        assert "_is_git_folder" not in source, (
            "_create_watch_handler must not call _is_git_folder (Story #226)"
        )

    def test_create_watch_handler_no_bug177_workaround(self):
        """_create_watch_handler() must not have Bug #177 workaround for non-git folders."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        source = inspect.getsource(DaemonWatchManager._create_watch_handler)

        assert "Bug #177" not in source, (
            "_create_watch_handler must not have Bug #177 workaround (Story #226)"
        )
        assert "Non-git folder" not in source, (
            "_create_watch_handler must not have non-git folder special case (Story #226)"
        )

    def test_create_watch_handler_always_creates_git_aware_handler(self):
        """_create_watch_handler() source must reference GitAwareWatchHandler."""
        from code_indexer.daemon.watch_manager import DaemonWatchManager

        source = inspect.getsource(DaemonWatchManager._create_watch_handler)

        assert "GitAwareWatchHandler" in source, (
            "_create_watch_handler must always create GitAwareWatchHandler (Story #226)"
        )


class TestDeadCodeAudit:
    """C8: Comprehensive dead code audit — verify all references are eliminated."""

    def test_no_langfuse_watch_integration_reference_in_watch_manager(self):
        """watch_manager.py must have zero references to langfuse_watch_integration."""
        watch_manager_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "daemon"
            / "watch_manager.py"
        )
        source = watch_manager_path.read_text()
        assert "langfuse_watch_integration" not in source

    def test_no_simple_watch_handler_import_in_watch_manager(self):
        """watch_manager.py must not import SimpleWatchHandler."""
        watch_manager_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "daemon"
            / "watch_manager.py"
        )
        source = watch_manager_path.read_text()
        assert (
            "from code_indexer.services.simple_watch_handler import SimpleWatchHandler"
            not in source
        )

    def test_no_create_simple_watch_handler_anywhere_in_watch_manager(self):
        """watch_manager.py must not define or call _create_simple_watch_handler."""
        watch_manager_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "daemon"
            / "watch_manager.py"
        )
        source = watch_manager_path.read_text()
        assert "_create_simple_watch_handler" not in source

    def test_no_subprocess_import_in_register_langfuse_golden_repos(self):
        """register_langfuse_golden_repos() must not have 'import subprocess' inside it."""
        app_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "app.py"
        )
        source = app_path.read_text()

        # Extract register_langfuse_golden_repos function body
        lines = source.splitlines()
        in_function = False
        function_lines = []

        for line in lines:
            if "def register_langfuse_golden_repos" in line:
                in_function = True
            elif in_function and line.strip() and not line.startswith(" "):
                # Top-level definition started — left the function
                break
            if in_function:
                function_lines.append(line)

        function_source = "\n".join(function_lines)

        assert "import subprocess" not in function_source, (
            "register_langfuse_golden_repos() must not contain 'import subprocess' (Story #226)"
        )

    def test_no_auto_watch_manager_in_on_langfuse_sync_complete(self):
        """app.py _on_langfuse_sync_complete must not reference auto_watch_manager."""
        app_path = (
            Path(__file__).parent.parent.parent.parent
            / "src"
            / "code_indexer"
            / "server"
            / "app.py"
        )
        source = app_path.read_text()

        # Extract _on_langfuse_sync_complete function body
        lines = source.splitlines()
        in_function = False
        function_lines = []
        base_indent = None

        for line in lines:
            if "def _on_langfuse_sync_complete" in line:
                in_function = True
                base_indent = len(line) - len(line.lstrip())
                function_lines.append(line)
                continue
            if in_function:
                if line.strip() == "":
                    function_lines.append(line)
                    continue
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= base_indent and line.strip():
                    break
                function_lines.append(line)

        function_source = "\n".join(function_lines)

        assert "auto_watch_manager" not in function_source, (
            "_on_langfuse_sync_complete must NOT reference auto_watch_manager (Story #226). "
            f"Function body:\n{function_source}"
        )
