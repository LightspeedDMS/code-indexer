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
from code_indexer.config import VectorStoreConfig


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


class TestFTSWatchHandlerAttachment:
    """Test FTS watch handler attachment to SimpleWatchHandler."""

    def test_fts_handler_attached_when_tantivy_index_exists(self):
        """Test FTS handler is attached when tantivy_index directory exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder
            # Create FTS index directory (primary location)
            fts_index_dir = project_path / ".code-indexer" / "tantivy_index"
            fts_index_dir.mkdir(parents=True)

            manager = DaemonWatchManager()

            # Mock everything except SimpleWatchHandler (let it be real)
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
                            # Mock TantivyIndexManager and FTSWatchHandler
                            with patch(
                                "code_indexer.services.tantivy_index_manager.TantivyIndexManager"
                            ) as mock_tantivy_mgr:
                                mock_tantivy_instance = MagicMock()
                                mock_tantivy_mgr.return_value = mock_tantivy_instance

                                with patch(
                                    "code_indexer.services.fts_watch_handler.FTSWatchHandler"
                                ) as mock_fts_handler:
                                    mock_fts_instance = MagicMock()
                                    mock_fts_handler.return_value = mock_fts_instance

                                    # Create handler
                                    handler = manager._create_watch_handler(
                                        str(project_path), mock_config
                                    )

                                    # Verify TantivyIndexManager was initialized
                                    mock_tantivy_mgr.assert_called_once_with(
                                        fts_index_dir
                                    )
                                    mock_tantivy_instance.initialize_index.assert_called_once_with(
                                        create_new=False
                                    )

                                    # Verify FTSWatchHandler was created
                                    mock_fts_handler.assert_called_once()
                                    call_kwargs = mock_fts_handler.call_args.kwargs
                                    assert "tantivy_index_manager" in call_kwargs
                                    assert "config" in call_kwargs

                                    # Verify handler has additional_handlers set
                                    assert hasattr(handler, "additional_handlers")
                                    assert len(handler.additional_handlers) == 1
                                    assert (
                                        handler.additional_handlers[0]
                                        == mock_fts_instance
                                    )

    def test_fts_handler_not_attached_when_no_index(self):
        """Test FTS handler is NOT attached when no FTS index exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder, NO FTS index

            manager = DaemonWatchManager()

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
                            # Mock FTS components (should NOT be called)
                            with patch(
                                "code_indexer.services.tantivy_index_manager.TantivyIndexManager"
                            ) as mock_tantivy_mgr:
                                with patch(
                                    "code_indexer.services.fts_watch_handler.FTSWatchHandler"
                                ) as mock_fts_handler:
                                    # Create handler
                                    handler = manager._create_watch_handler(
                                        str(project_path), mock_config
                                    )

                                    # Verify FTS components were NOT initialized
                                    mock_tantivy_mgr.assert_not_called()
                                    mock_fts_handler.assert_not_called()

                                    # Verify handler has empty additional_handlers
                                    assert hasattr(handler, "additional_handlers")
                                    assert len(handler.additional_handlers) == 0

    def test_fts_handler_attached_with_alternative_index_path(self):
        """Test FTS handler is attached with alternative tantivy-fts path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder
            # Create FTS index directory (alternative location)
            fts_index_dir = (
                project_path / ".code-indexer" / "index" / "tantivy-fts"
            )
            fts_index_dir.mkdir(parents=True)

            manager = DaemonWatchManager()

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
                                "code_indexer.services.tantivy_index_manager.TantivyIndexManager"
                            ) as mock_tantivy_mgr:
                                mock_tantivy_instance = MagicMock()
                                mock_tantivy_mgr.return_value = mock_tantivy_instance

                                with patch(
                                    "code_indexer.services.fts_watch_handler.FTSWatchHandler"
                                ) as mock_fts_handler:
                                    mock_fts_instance = MagicMock()
                                    mock_fts_handler.return_value = mock_fts_instance

                                    # Create handler
                                    handler = manager._create_watch_handler(
                                        str(project_path), mock_config
                                    )

                                    # Verify TantivyIndexManager was initialized with alternative path
                                    mock_tantivy_mgr.assert_called_once_with(
                                        fts_index_dir
                                    )

                                    # Verify FTS handler attached
                                    assert hasattr(handler, "additional_handlers")
                                    assert len(handler.additional_handlers) == 1


class TestNonGitFolderWithoutVectorStoreConfig:
    """Test Bug #177: Non-git folders without vector_store config should not crash."""

    def test_non_git_folder_without_vector_store_config_does_not_crash(self):
        """
        Test that non-git folders (Langfuse) without vector_store config don't crash.

        Bug #177: WatchManager._create_watch_handler() crashes with
        "missing vector_store field" when watching Langfuse folders because:
        1. Langfuse folders don't have .code-indexer/config.yaml
        2. config.vector_store is None
        3. BackendFactory.create() raises ValueError at line 37

        This test reproduces the bug and verifies the fix.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder (no .git directory)

            manager = DaemonWatchManager()

            # Create a config with vector_store = None (reproduces Bug #177)
            with patch("code_indexer.config.ConfigManager") as mock_config_mgr:
                mock_config = MagicMock()
                mock_config.codebase_dir = project_path
                mock_config.vector_store = None  # Bug #177 trigger
                mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                    mock_config
                )
                mock_config_mgr.create_with_backtrack.return_value.config_path = (
                    project_path / ".code-indexer" / "config.yaml"
                )

                with patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ):
                    with patch(
                        "code_indexer.services.simple_watch_handler.SimpleWatchHandler"
                    ) as mock_simple_handler:
                        mock_simple_handler.return_value = MagicMock()

                        # This should NOT raise ValueError
                        # Before fix: raises "Invalid configuration: missing vector_store field"
                        # After fix: creates default filesystem backend for non-git folders
                        handler = manager._create_watch_handler(
                            str(project_path), mock_config
                        )

                        # Verify SimpleWatchHandler was created successfully
                        assert handler is not None
                        mock_simple_handler.assert_called_once()

    def test_non_git_folder_creates_default_filesystem_backend(self):
        """
        Test that non-git folders without vector_store get default filesystem backend.

        The fix should initialize config.vector_store with VectorStoreConfig
        when it's None for non-git folders, ensuring BackendFactory receives
        a valid configuration.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Non-git folder

            manager = DaemonWatchManager()

            with patch("code_indexer.config.ConfigManager") as mock_config_mgr:
                mock_config = MagicMock()
                mock_config.codebase_dir = project_path
                mock_config.vector_store = None  # Initially None
                mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                    mock_config
                )
                mock_config_mgr.create_with_backtrack.return_value.config_path = (
                    project_path / ".code-indexer" / "config.yaml"
                )

                with patch(
                    "code_indexer.backends.backend_factory.BackendFactory"
                ) as mock_backend_factory:
                    mock_backend = MagicMock()
                    mock_backend_factory.create.return_value = mock_backend

                    with patch(
                        "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                    ):
                        with patch("code_indexer.services.smart_indexer.SmartIndexer"):
                            with patch(
                                "code_indexer.services.simple_watch_handler.SimpleWatchHandler"
                            ) as mock_simple_handler:
                                mock_simple_handler.return_value = MagicMock()

                                # Call _create_watch_handler
                                handler = manager._create_watch_handler(
                                    str(project_path), mock_config
                                )

                                # Verify config.vector_store was initialized
                                assert mock_config.vector_store is not None
                                assert mock_config.vector_store.provider == "filesystem"

                                # Verify BackendFactory.create was called with valid config
                                mock_backend_factory.create.assert_called_once()

                                # Verify handler created successfully
                                assert handler is not None

    def test_git_folder_with_vector_store_still_works(self):
        """
        Test that git folders with existing vector_store config still work normally.

        Regression test: ensure the Bug #177 fix doesn't break existing git folder behavior.
        Git folders with proper config.vector_store should continue to create
        GitAwareWatchHandler as before.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            project_path = Path(tmpdir)
            # Create .git directory to make it a git repo
            git_dir = project_path / ".git"
            git_dir.mkdir()

            manager = DaemonWatchManager()

            # Mock config with vector_store set (normal case)
            with patch("code_indexer.config.ConfigManager") as mock_config_mgr:
                mock_config = MagicMock()
                mock_config.codebase_dir = project_path
                mock_config.vector_store = VectorStoreConfig(provider="filesystem")
                mock_config_mgr.create_with_backtrack.return_value.get_config.return_value = (
                    mock_config
                )
                mock_config_mgr.create_with_backtrack.return_value.config_path = (
                    project_path / ".code-indexer" / "config.yaml"
                )

                with patch("code_indexer.backends.backend_factory.BackendFactory"):
                    with patch(
                        "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                    ):
                        with patch("code_indexer.services.smart_indexer.SmartIndexer"):
                            with patch(
                                "code_indexer.services.git_aware_watch_handler.GitAwareWatchHandler"
                            ) as mock_git_handler:
                                mock_git_handler.return_value = MagicMock()

                                with patch(
                                    "code_indexer.services.git_topology_service.GitTopologyService"
                                ):
                                    with patch(
                                        "code_indexer.services.watch_metadata.WatchMetadata"
                                    ):
                                        # Should create GitAwareWatchHandler successfully
                                        handler = manager._create_watch_handler(
                                            str(project_path), mock_config
                                        )

                                        # Verify GitAwareWatchHandler was created
                                        mock_git_handler.assert_called_once()
                                        assert handler is not None
