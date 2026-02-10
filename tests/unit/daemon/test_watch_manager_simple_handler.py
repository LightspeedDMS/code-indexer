"""
Tests for DaemonWatchManager's support for SimpleWatchHandler.

Tests automatic selection of SimpleWatchHandler for non-git folders
and GitAwareWatchHandler for git folders.
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from code_indexer.daemon.watch_manager import DaemonWatchManager


class TestWatchHandlerSelection:
    """Test automatic handler selection based on folder type."""

    def test_creates_git_aware_handler_for_git_repo(self):
        """Test GitAwareWatchHandler is created for git repositories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Create .git directory to make it a git repo
            git_dir = project_path / ".git"
            git_dir.mkdir()

            manager = DaemonWatchManager()

            # Mock the handler classes to avoid full initialization
            # Patch in their original module locations (lazy imports)
            with patch(
                "code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler"
            ) as mock_git_handler:
                mock_git_handler.return_value = MagicMock()

                with patch(
                    "code_indexer.config.ConfigManager"
                ) as mock_config_mgr:
                    mock_config = MagicMock()
                    mock_config.codebase_dir = project_path
                    mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                        mock_config
                    )

                    with patch(
                        "code_indexer.backends.backend_factory.BackendFactory"
                    ):
                        with patch(
                            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                        ):
                            with patch(
                                "code_indexer.services.smart_indexer.SmartIndexer"
                            ):
                                with patch(
                                    "code_indexer.services.git_topology_service.GitTopologyService"
                                ):
                                    with patch(
                                        "code_indexer.services.watch_metadata.WatchMetadata"
                                    ):
                                        # Call _create_watch_handler
                                        handler = manager._create_watch_handler(
                                            str(project_path), mock_config
                                        )

                                        # Verify GitAwareWatchHandler was instantiated
                                        mock_git_handler.assert_called_once()
                                        assert handler is not None

    def test_creates_simple_handler_for_non_git_folder(self):
        """Test SimpleWatchHandler is created for non-git folders."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # No .git directory - non-git folder

            manager = DaemonWatchManager()

            # Mock both handler classes in their original modules
            with patch(
                "code_indexer.services.simple_watch_handler.SimpleWatchHandler"
            ) as mock_simple_handler:
                mock_simple_handler.return_value = MagicMock()

                with patch(
                    "code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler"
                ) as mock_git_handler:
                    mock_git_handler.return_value = MagicMock()

                    with patch(
                        "code_indexer.config.ConfigManager"
                    ) as mock_config_mgr:
                        mock_config = MagicMock()
                        mock_config.codebase_dir = project_path
                        mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                            mock_config
                        )

                        with patch(
                            "code_indexer.backends.backend_factory.BackendFactory"
                        ):
                            with patch(
                                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                            ):
                                with patch(
                                    "code_indexer.services.smart_indexer.SmartIndexer"
                                ):
                                    # Call _create_watch_handler
                                    handler = manager._create_watch_handler(
                                        str(project_path), mock_config
                                    )

                                    # Verify SimpleWatchHandler was instantiated, not GitAwareWatchHandler
                                    mock_simple_handler.assert_called_once()
                                    mock_git_handler.assert_not_called()
                                    assert handler is not None

    def test_handler_selection_uses_git_directory_check(self):
        """Test handler selection is based on presence of .git directory."""
        manager = DaemonWatchManager()

        # Test with git folder
        with tempfile.TemporaryDirectory() as tmpdir:
            git_folder = Path(tmpdir) / "git_repo"
            git_folder.mkdir()
            (git_folder / ".git").mkdir()

            is_git = manager._is_git_folder(str(git_folder))
            assert is_git is True

        # Test with non-git folder
        with tempfile.TemporaryDirectory() as tmpdir:
            non_git_folder = Path(tmpdir) / "non_git_folder"
            non_git_folder.mkdir()

            is_git = manager._is_git_folder(str(non_git_folder))
            assert is_git is False


class TestSimpleHandlerCallbackWiring:
    """Test SimpleWatchHandler receives correct callback."""

    def test_simple_handler_receives_indexing_callback(self):
        """Test SimpleWatchHandler is initialized with indexing callback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder

            manager = DaemonWatchManager()

            with patch(
                "code_indexer.services.simple_watch_handler.SimpleWatchHandler"
            ) as mock_simple_handler:
                mock_handler_instance = MagicMock()
                mock_simple_handler.return_value = mock_handler_instance

                with patch(
                    "code_indexer.config.ConfigManager"
                ) as mock_config_mgr:
                    mock_config = MagicMock()
                    mock_config.codebase_dir = project_path
                    mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                        mock_config
                    )

                    with patch(
                        "code_indexer.backends.backend_factory.BackendFactory"
                    ):
                        with patch(
                            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                        ):
                            with patch(
                                "code_indexer.services.smart_indexer.SmartIndexer"
                            ):
                                handler = manager._create_watch_handler(
                                    str(project_path), mock_config
                                )

                                # Verify SimpleWatchHandler was called with correct arguments
                                mock_simple_handler.assert_called_once()
                                call_kwargs = mock_simple_handler.call_args.kwargs

                                # Should have folder_path and indexing_callback
                                assert "folder_path" in call_kwargs
                                assert "indexing_callback" in call_kwargs
                                assert callable(call_kwargs["indexing_callback"])

    def test_simple_handler_callback_triggers_smart_indexer(self):
        """Test callback from SimpleWatchHandler triggers SmartIndexer."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Create a test file
            test_file = project_path / "test.json"
            test_file.write_text("{}")

            manager = DaemonWatchManager()

            # Mock SmartIndexer
            with patch(
                "code_indexer.services.smart_indexer.SmartIndexer"
            ) as mock_smart_indexer_class:
                mock_smart_indexer = MagicMock()
                mock_smart_indexer_class.return_value = mock_smart_indexer

                with patch(
                    "code_indexer.services.simple_watch_handler.SimpleWatchHandler"
                ) as mock_simple_handler:
                    # Capture the callback
                    captured_callback = None

                    def capture_callback(*args, **kwargs):
                        nonlocal captured_callback
                        captured_callback = kwargs.get("indexing_callback")
                        return MagicMock()

                    mock_simple_handler.side_effect = capture_callback

                    with patch(
                        "code_indexer.config.ConfigManager"
                    ) as mock_config_mgr:
                        mock_config = MagicMock()
                        mock_config.codebase_dir = project_path
                        mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                            mock_config
                        )

                        with patch(
                            "code_indexer.backends.backend_factory.BackendFactory"
                        ):
                            with patch(
                                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                            ):
                                # Create handler
                                handler = manager._create_watch_handler(
                                    str(project_path), mock_config
                                )

                                # Verify callback was captured
                                assert captured_callback is not None

                                # Call the callback with test file
                                captured_callback([str(test_file)], "created")

                                # Verify SmartIndexer.process_files_incrementally was called
                                mock_smart_indexer.process_files_incrementally.assert_called_once()
