"""
Unit tests for LangfuseWatchIntegration service.

Tests the integration between SimpleWatchHandler and the CIDX indexing pipeline
for Langfuse trace folders.
"""

import pytest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from code_indexer.server.services.langfuse_watch_integration import (
    LangfuseWatchIntegration,
)


class TestLangfuseWatchIntegrationInit:
    """Test service initialization."""

    def test_initialization_with_auto_watch_manager(self):
        """Test service can be initialized with an AutoWatchManager."""
        auto_watch_manager = MagicMock()

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        assert integration.auto_watch_manager is auto_watch_manager


class TestAutoStartWatching:
    """Test auto-start behavior when files are written."""

    def test_start_watching_on_first_file_write(self):
        """Test watching starts when first file is written to a folder."""
        auto_watch_manager = MagicMock()
        auto_watch_manager.is_watching.return_value = False
        auto_watch_manager.start_watch.return_value = {
            "status": "success",
            "message": "Watch started",
        }

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)

            # Simulate file write notification
            integration.on_file_written(folder)

            # Verify start_watch was called
            auto_watch_manager.start_watch.assert_called_once()
            call_args = auto_watch_manager.start_watch.call_args
            assert str(folder) in str(call_args)

    def test_does_not_restart_if_already_watching(self):
        """Test watching does not restart if already active."""
        auto_watch_manager = MagicMock()
        auto_watch_manager.is_watching.return_value = True

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)

            integration.on_file_written(folder)

            # Should not call start_watch since already watching
            auto_watch_manager.start_watch.assert_not_called()
            # Should call reset_timeout instead
            auto_watch_manager.reset_timeout.assert_called_once()

    def test_multiple_folders_can_be_watched_simultaneously(self):
        """Test multiple Langfuse folders can be watched at the same time."""
        auto_watch_manager = MagicMock()
        auto_watch_manager.is_watching.return_value = False
        auto_watch_manager.start_watch.return_value = {
            "status": "success",
            "message": "Watch started",
        }

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder1 = Path(tmpdir) / "langfuse_project1_user1"
            folder2 = Path(tmpdir) / "langfuse_project2_user2"
            folder1.mkdir()
            folder2.mkdir()

            # Write to both folders
            integration.on_file_written(folder1)
            integration.on_file_written(folder2)

            # Both should have watch started
            assert auto_watch_manager.start_watch.call_count == 2

            # Verify both paths were passed
            call_paths = [
                str(call.args[0]) if call.args else str(call.kwargs.get("repo_path", ""))
                for call in auto_watch_manager.start_watch.call_args_list
            ]
            assert str(folder1) in call_paths[0]
            assert str(folder2) in call_paths[1]


class TestFolderDetection:
    """Test detection of git vs non-git folders."""

    def test_detects_non_git_folder(self):
        """Test service correctly identifies non-git folder."""
        auto_watch_manager = MagicMock()
        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)

            # Folder without .git
            assert integration.has_git_directory(folder) is False

    def test_detects_git_folder(self):
        """Test service correctly identifies git folder."""
        auto_watch_manager = MagicMock()
        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            git_dir = folder / ".git"
            git_dir.mkdir()

            # Folder with .git
            assert integration.has_git_directory(folder) is True

    def test_detects_langfuse_folder_by_name(self):
        """Test service identifies Langfuse folders by naming convention."""
        auto_watch_manager = MagicMock()
        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            langfuse_folder = base / "langfuse_project_user"
            langfuse_folder.mkdir()

            assert integration.is_langfuse_folder(langfuse_folder) is True

    def test_non_langfuse_folder_not_detected(self):
        """Test non-Langfuse folders are not identified as Langfuse folders."""
        auto_watch_manager = MagicMock()
        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "regular_folder"
            folder.mkdir()

            assert integration.is_langfuse_folder(folder) is False


class TestWatchStatus:
    """Test watch status reporting."""

    def test_get_watch_status_for_folder(self):
        """Test can retrieve watch status for a specific folder."""
        auto_watch_manager = MagicMock()
        auto_watch_manager.get_state.return_value = {
            "watch_running": True,
            "last_activity": "2026-02-09T12:00:00",
            "timeout_seconds": 300,
        }

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)

            status = integration.get_watch_status(folder)

            assert status is not None
            assert status["watch_running"] is True
            assert "last_activity" in status

    def test_get_watch_status_returns_none_if_not_watching(self):
        """Test get_watch_status returns None if folder is not being watched."""
        auto_watch_manager = MagicMock()
        auto_watch_manager.get_state.return_value = None

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)

            status = integration.get_watch_status(folder)

            assert status is None

    def test_get_all_watch_statuses(self):
        """Test can retrieve status for all watched folders."""
        auto_watch_manager = MagicMock()

        # Mock the public get_all_states() method
        auto_watch_manager.get_all_states.return_value = {
            "/path/folder1": {
                "watch_running": True,
                "last_activity": "2026-02-09T12:00:00",
                "timeout_seconds": 300,
            },
            "/path/folder2": {
                "watch_running": True,
                "last_activity": "2026-02-09T12:05:00",
                "timeout_seconds": 300,
            },
        }

        integration = LangfuseWatchIntegration(auto_watch_manager=auto_watch_manager)

        all_statuses = integration.get_all_watch_statuses()

        # Should return information about all watched folders
        assert len(all_statuses) == 2
        assert "/path/folder1" in all_statuses
        assert "/path/folder2" in all_statuses
        assert all_statuses["/path/folder1"]["watch_running"] is True
        assert all_statuses["/path/folder1"]["timeout_seconds"] == 300
        assert all_statuses["/path/folder2"]["watch_running"] is True
        assert all_statuses["/path/folder2"]["timeout_seconds"] == 300
