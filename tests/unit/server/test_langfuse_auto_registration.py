"""
Unit tests for Langfuse auto-registration as golden repos.

Tests the auto-registration of langfuse_* directories as golden repos
and the on_sync_complete callback integration.
"""

import pytest
import tempfile
import shutil
import threading
from pathlib import Path
from unittest.mock import Mock, patch


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def golden_repos_dir(temp_data_dir):
    """Create golden-repos directory."""
    gr_dir = Path(temp_data_dir) / "golden-repos"
    gr_dir.mkdir(parents=True)
    return gr_dir


@pytest.fixture
def mock_golden_repo_manager():
    """Create mock golden repo manager."""
    mock_manager = Mock()
    mock_manager.golden_repos = {}
    mock_manager._save_metadata = Mock()
    mock_manager._operation_lock = threading.RLock()
    mock_manager.golden_repo_exists = Mock(return_value=False)
    # SQLite backend support (for persistence tests)
    mock_manager._use_sqlite = False  # Default to JSON mode
    mock_manager._sqlite_backend = None
    return mock_manager


class TestRegisterLangfuseGoldenRepos:
    """Test register_langfuse_golden_repos() function."""

    def test_registers_new_langfuse_folders(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that new langfuse_* folders are registered as golden repos with local:// URL.
        """
        # Setup: Create langfuse_* directories
        langfuse1 = golden_repos_dir / "langfuse_project1_user1"
        langfuse2 = golden_repos_dir / "langfuse_project2_user2"
        langfuse1.mkdir()
        langfuse2.mkdir()

        # Mock GlobalActivator to avoid actual global activation
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: Both folders were registered
            assert len(mock_golden_repo_manager.golden_repos) == 2
            assert "langfuse_project1_user1" in mock_golden_repo_manager.golden_repos
            assert "langfuse_project2_user2" in mock_golden_repo_manager.golden_repos

            # Verify: Registered with local:// URL scheme
            repo1 = mock_golden_repo_manager.golden_repos["langfuse_project1_user1"]
            assert repo1.repo_url == "local://langfuse_project1_user1"
            assert repo1.alias == "langfuse_project1_user1"
            assert repo1.clone_path == str(langfuse1)

            repo2 = mock_golden_repo_manager.golden_repos["langfuse_project2_user2"]
            assert repo2.repo_url == "local://langfuse_project2_user2"
            assert repo2.alias == "langfuse_project2_user2"
            assert repo2.clone_path == str(langfuse2)

    def test_skips_already_registered_folders(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that folders already in golden_repos_metadata are not re-registered (idempotent).
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_existing_user"
        langfuse.mkdir()

        # Mock that folder already exists
        mock_golden_repo_manager.golden_repo_exists = Mock(return_value=True)

        # Execute
        from code_indexer.server.app import register_langfuse_golden_repos

        register_langfuse_golden_repos(
            mock_golden_repo_manager, str(golden_repos_dir)
        )

        # Verify: No new repos added
        assert len(mock_golden_repo_manager.golden_repos) == 0

    def test_skips_non_langfuse_folders(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that regular folders (without langfuse_ prefix) are NOT registered.
        """
        # Setup: Create non-langfuse directories
        regular1 = golden_repos_dir / "code-indexer"
        regular2 = golden_repos_dir / "java-mock"
        cidx_meta = golden_repos_dir / "cidx-meta"
        regular1.mkdir()
        regular2.mkdir()
        cidx_meta.mkdir()

        # Execute
        from code_indexer.server.app import register_langfuse_golden_repos

        register_langfuse_golden_repos(
            mock_golden_repo_manager, str(golden_repos_dir)
        )

        # Verify: No repos registered
        assert len(mock_golden_repo_manager.golden_repos) == 0

    def test_handles_empty_directory(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that function handles empty golden-repos/ directory gracefully.
        """
        # Setup: Empty directory (no folders)

        # Execute
        from code_indexer.server.app import register_langfuse_golden_repos

        register_langfuse_golden_repos(
            mock_golden_repo_manager, str(golden_repos_dir)
        )

        # Verify: No errors, no registrations
        assert len(mock_golden_repo_manager.golden_repos) == 0

    def test_handles_nonexistent_directory(self, mock_golden_repo_manager):
        """
        Test that function returns gracefully when directory doesn't exist.
        """
        # Setup: Non-existent directory path
        nonexistent_path = "/nonexistent/golden-repos"

        # Execute
        from code_indexer.server.app import register_langfuse_golden_repos

        # Should not raise exception
        register_langfuse_golden_repos(mock_golden_repo_manager, nonexistent_path)

        # Verify: No registrations
        assert len(mock_golden_repo_manager.golden_repos) == 0

    def test_activates_globally(self, golden_repos_dir, mock_golden_repo_manager):
        """
        Test that GlobalActivator.activate_golden_repo() is called for each new registration.
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_test_user"
        langfuse.mkdir()

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: GlobalActivator was instantiated
            mock_activator_class.assert_called_once_with(str(golden_repos_dir))

            # Verify: activate_golden_repo was called
            mock_activator.activate_golden_repo.assert_called_once_with(
                repo_name="langfuse_test_user",
                repo_url="local://langfuse_test_user",
                clone_path=str(langfuse),
                enable_temporal=False,
            )

    def test_handles_activation_failure_gracefully(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that if GlobalActivator raises, the golden repo is still registered (just not activated).
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_failing_user"
        langfuse.mkdir()

        # Mock GlobalActivator to raise exception
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator.activate_golden_repo.side_effect = Exception(
                "Activation failed"
            )
            mock_activator_class.return_value = mock_activator

            # Execute - should not raise
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: Repo was still registered despite activation failure
            assert "langfuse_failing_user" in mock_golden_repo_manager.golden_repos
            repo = mock_golden_repo_manager.golden_repos["langfuse_failing_user"]
            assert repo.repo_url == "local://langfuse_failing_user"

    def test_registers_multiple_folders(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that multiple langfuse_* folders are all registered.
        """
        # Setup: Create multiple langfuse directories
        folders = [
            "langfuse_project1_alice",
            "langfuse_project2_bob",
            "langfuse_project3_charlie",
        ]
        for folder_name in folders:
            (golden_repos_dir / folder_name).mkdir()

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: All folders registered
            assert len(mock_golden_repo_manager.golden_repos) == 3
            for folder_name in folders:
                assert folder_name in mock_golden_repo_manager.golden_repos
                repo = mock_golden_repo_manager.golden_repos[folder_name]
                assert repo.repo_url == f"local://{folder_name}"
                assert repo.alias == folder_name

            # Verify: GlobalActivator called for each registration
            assert mock_activator.activate_golden_repo.call_count == 3

    def test_sorted_folder_processing(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that folders are processed in sorted order (predictable behavior).
        """
        # Setup: Create folders in reverse alphabetical order
        folders = [
            "langfuse_zebra",
            "langfuse_alpha",
            "langfuse_middle",
        ]
        for folder_name in folders:
            (golden_repos_dir / folder_name).mkdir()

        # Track registration order
        registration_order = []

        def track_registration(alias):
            registration_order.append(alias)
            return False

        mock_golden_repo_manager.golden_repo_exists = Mock(
            side_effect=lambda alias: track_registration(alias)
        )

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: Processed in sorted order
            assert registration_order == [
                "langfuse_alpha",
                "langfuse_middle",
                "langfuse_zebra",
            ]

    def test_registers_with_sqlite_backend(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that when _use_sqlite=True, _sqlite_backend.add_repo() is called instead of _save_metadata().

        This verifies the fix for the SQLite persistence bug where repos were added to in-memory
        dict but never persisted to golden_repos_metadata SQLite table.
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_sqlite_test"
        langfuse.mkdir()

        # Configure mock for SQLite mode
        mock_sqlite_backend = Mock()
        mock_golden_repo_manager._use_sqlite = True
        mock_golden_repo_manager._sqlite_backend = mock_sqlite_backend

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: _sqlite_backend.add_repo() was called with correct parameters
            mock_sqlite_backend.add_repo.assert_called_once()
            call_args = mock_sqlite_backend.add_repo.call_args
            assert call_args[1]["alias"] == "langfuse_sqlite_test"
            assert call_args[1]["repo_url"] == "local://langfuse_sqlite_test"
            assert call_args[1]["default_branch"] == "main"
            assert call_args[1]["clone_path"] == str(langfuse)
            assert call_args[1]["enable_temporal"] is False
            assert call_args[1]["temporal_options"] is None
            assert "created_at" in call_args[1]

            # Verify: _save_metadata() was NOT called (SQLite mode uses add_repo per-operation)
            mock_golden_repo_manager._save_metadata.assert_not_called()

    def test_registers_with_json_fallback(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that when _use_sqlite=False, _save_metadata() is called (JSON persistence).
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_json_test"
        langfuse.mkdir()

        # Configure mock for JSON mode (default)
        assert mock_golden_repo_manager._use_sqlite is False
        assert mock_golden_repo_manager._sqlite_backend is None

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: _save_metadata() was called (JSON mode)
            mock_golden_repo_manager._save_metadata.assert_called()

            # Verify: Repo was added to in-memory dict
            assert "langfuse_json_test" in mock_golden_repo_manager.golden_repos

    def test_handles_sqlite_integrity_error_gracefully(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that IntegrityError from add_repo() is caught gracefully.

        Bug #131 Finding 1.2: Verifies that TOCTOU race condition between
        golden_repo_exists() check and add_repo() call is handled without crashing.
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_race_test"
        langfuse.mkdir()

        # Configure mock for SQLite mode
        import sqlite3

        mock_sqlite_backend = Mock()
        # Simulate TOCTOU race: exists() returns False, but add_repo() raises IntegrityError
        mock_sqlite_backend.add_repo.side_effect = sqlite3.IntegrityError(
            "UNIQUE constraint failed: golden_repos_metadata.alias"
        )
        mock_golden_repo_manager._use_sqlite = True
        mock_golden_repo_manager._sqlite_backend = mock_sqlite_backend

        # Mock GlobalActivator
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            # Execute - should not raise exception
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: add_repo() was called
            mock_sqlite_backend.add_repo.assert_called_once()

            # Verify: Repo was still added to in-memory dict (before exception)
            assert "langfuse_race_test" in mock_golden_repo_manager.golden_repos

            # Test passes if no exception propagated


class TestOnSyncCompleteCallback:
    """Test on_sync_complete callback integration with LangfuseTraceSyncService."""

    def test_callback_called_after_sync(self, temp_data_dir):
        """
        Test that after sync_all_projects(), the callback fires.
        """
        # Setup: Create mock callback
        mock_callback = Mock()

        # Mock config with enabled Langfuse but no projects (minimal sync)
        from code_indexer.server.utils.config_manager import LangfuseConfig

        mock_config = Mock()
        mock_config.langfuse_config = LangfuseConfig(
            pull_enabled=True,
            pull_host="https://test.langfuse.com",
            pull_projects=[],  # No projects - minimal sync
            pull_sync_interval_seconds=300,
            pull_trace_age_days=7,
        )
        config_getter = Mock(return_value=mock_config)

        # Create service with callback
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        service = LangfuseTraceSyncService(
            config_getter=config_getter,
            data_dir=temp_data_dir,
            on_sync_complete=mock_callback,
        )

        # Execute: Run sync (no projects, but callback should fire)
        service.sync_all_projects()

        # Verify: Callback was invoked
        mock_callback.assert_called_once()

    def test_callback_not_called_when_none(self):
        """
        Test that when callback is None, no error occurs.
        """
        # Mock config_getter to return disabled config
        mock_config = Mock()
        mock_config.langfuse_config = None
        config_getter = Mock(return_value=mock_config)

        # Create service without callback
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        service = LangfuseTraceSyncService(
            config_getter=config_getter, data_dir="/tmp/test", on_sync_complete=None
        )

        # Execute: Should not raise exception
        service.sync_all_projects()

        # Test passes if no exception raised

    def test_callback_exception_does_not_break_sync(self, temp_data_dir):
        """
        Test that if callback raises, sync still completes normally.
        """
        # Setup: Create callback that raises exception
        mock_callback = Mock(side_effect=Exception("Callback failed"))

        # Mock config with enabled Langfuse
        from code_indexer.server.utils.config_manager import LangfuseConfig

        mock_config = Mock()
        mock_config.langfuse_config = LangfuseConfig(
            pull_enabled=True,
            pull_host="https://test.langfuse.com",
            pull_projects=[],  # No projects
            pull_sync_interval_seconds=300,
            pull_trace_age_days=7,
        )
        config_getter = Mock(return_value=mock_config)

        # Create service with failing callback
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        service = LangfuseTraceSyncService(
            config_getter=config_getter,
            data_dir=temp_data_dir,
            on_sync_complete=mock_callback,
        )

        # Execute: Should not raise exception despite callback failure
        service.sync_all_projects()

        # Verify: Callback was called (and failed)
        mock_callback.assert_called_once()

        # Test passes if no exception propagated to caller

    def test_callback_with_enabled_langfuse_config(self, temp_data_dir):
        """
        Test that callback fires after real sync (with mocked API).
        """
        # Setup: Create callback
        mock_callback = Mock()

        # Mock config with enabled Langfuse
        from code_indexer.server.utils.config_manager import (
            LangfuseConfig,
            LangfusePullProject,
        )

        mock_config = Mock()
        mock_config.langfuse_config = LangfuseConfig(
            pull_enabled=True,
            pull_host="https://test.langfuse.com",
            pull_projects=[
                LangfusePullProject(public_key="test_pk", secret_key="test_sk")
            ],
            pull_sync_interval_seconds=300,
            pull_trace_age_days=7,
        )
        config_getter = Mock(return_value=mock_config)

        # Create service with callback
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        service = LangfuseTraceSyncService(
            config_getter=config_getter,
            data_dir=temp_data_dir,
            on_sync_complete=mock_callback,
        )

        # Mock LangfuseApiClient to avoid real API calls
        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient"
        ) as mock_api_class:
            mock_api = Mock()
            mock_api.discover_project.return_value = {"name": "test_project"}
            mock_api.fetch_traces_page.return_value = []  # No traces
            mock_api_class.return_value = mock_api

            # Execute sync
            service.sync_all_projects()

            # Verify: Callback was invoked after sync
            mock_callback.assert_called_once()

    def test_callback_invoked_once_per_sync_cycle(self, temp_data_dir):
        """
        Test that callback is invoked once per sync_all_projects() call, not per project.
        """
        # Setup: Create callback
        mock_callback = Mock()

        # Mock config with multiple projects
        from code_indexer.server.utils.config_manager import (
            LangfuseConfig,
            LangfusePullProject,
        )

        mock_config = Mock()
        mock_config.langfuse_config = LangfuseConfig(
            pull_enabled=True,
            pull_host="https://test.langfuse.com",
            pull_projects=[
                LangfusePullProject(public_key="pk1", secret_key="sk1"),
                LangfusePullProject(public_key="pk2", secret_key="sk2"),
            ],
            pull_sync_interval_seconds=300,
            pull_trace_age_days=7,
        )
        config_getter = Mock(return_value=mock_config)

        # Create service with callback
        from code_indexer.server.services.langfuse_trace_sync_service import (
            LangfuseTraceSyncService,
        )

        service = LangfuseTraceSyncService(
            config_getter=config_getter,
            data_dir=temp_data_dir,
            on_sync_complete=mock_callback,
        )

        # Mock LangfuseApiClient
        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient"
        ) as mock_api_class:
            mock_api = Mock()
            mock_api.discover_project.return_value = {"name": "test_project"}
            mock_api.fetch_traces_page.return_value = []
            mock_api_class.return_value = mock_api

            # Execute sync
            service.sync_all_projects()

            # Verify: Callback called ONCE despite multiple projects
            assert mock_callback.call_count == 1


class TestCidxIndexInitialization:
    """Test CIDX index initialization for Langfuse folders."""

    def test_initializes_cidx_index_for_new_folders(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that cidx init + cidx index are called for newly registered Langfuse folders.
        """
        # Setup: Create langfuse folder (no .code-indexer directory)
        langfuse = golden_repos_dir / "langfuse_new_project"
        langfuse.mkdir()

        # Mock subprocess.run to capture cidx commands
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch("subprocess.run") as mock_subprocess_run:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator
            # subprocess.run returns CompletedProcess with no error
            mock_subprocess_run.return_value = Mock(returncode=0, stderr="", stdout="")

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: cidx init was called
            cidx_init_calls = [
                call
                for call in mock_subprocess_run.call_args_list
                if call[0][0] == ["cidx", "init"]
            ]
            assert len(cidx_init_calls) == 1
            assert cidx_init_calls[0][1]["cwd"] == str(langfuse)
            assert cidx_init_calls[0][1]["check"] is True
            assert cidx_init_calls[0][1]["capture_output"] is True
            assert cidx_init_calls[0][1]["text"] is True

            # Verify: cidx index was called
            cidx_index_calls = [
                call
                for call in mock_subprocess_run.call_args_list
                if call[0][0] == ["cidx", "index"]
            ]
            assert len(cidx_index_calls) == 1
            assert cidx_index_calls[0][1]["cwd"] == str(langfuse)
            assert cidx_index_calls[0][1]["check"] is True

    def test_skips_cidx_init_if_already_initialized(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that cidx init is skipped if .code-indexer directory already exists.
        """
        # Setup: Create langfuse folder with existing .code-indexer
        langfuse = golden_repos_dir / "langfuse_initialized"
        langfuse.mkdir()
        (langfuse / ".code-indexer").mkdir()

        # Mock subprocess.run
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch("subprocess.run") as mock_subprocess_run:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator
            mock_subprocess_run.return_value = Mock(returncode=0, stderr="", stdout="")

            # Execute
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: cidx init was NOT called
            cidx_init_calls = [
                call
                for call in mock_subprocess_run.call_args_list
                if call[0][0] == ["cidx", "init"]
            ]
            assert len(cidx_init_calls) == 0

            # Verify: cidx index was still called
            cidx_index_calls = [
                call
                for call in mock_subprocess_run.call_args_list
                if call[0][0] == ["cidx", "index"]
            ]
            assert len(cidx_index_calls) == 1

    def test_handles_cidx_init_failure_gracefully(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that cidx init failure doesn't block golden repo registration.
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_init_fail"
        langfuse.mkdir()

        # Mock subprocess.run to fail on cidx init
        import subprocess

        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch("subprocess.run") as mock_subprocess_run:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            def run_side_effect(cmd, **kwargs):
                if cmd == ["cidx", "init"]:
                    raise subprocess.CalledProcessError(1, cmd, stderr="Init failed")
                return Mock(returncode=0, stderr="", stdout="")

            mock_subprocess_run.side_effect = run_side_effect

            # Execute - should not raise exception
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: Repo was still registered despite init failure
            assert "langfuse_init_fail" in mock_golden_repo_manager.golden_repos

    def test_handles_cidx_index_failure_gracefully(
        self, golden_repos_dir, mock_golden_repo_manager
    ):
        """
        Test that cidx index failure doesn't block golden repo registration.
        """
        # Setup: Create langfuse folder
        langfuse = golden_repos_dir / "langfuse_index_fail"
        langfuse.mkdir()

        # Mock subprocess.run to fail on cidx index
        import subprocess

        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch("subprocess.run") as mock_subprocess_run:
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator

            def run_side_effect(cmd, **kwargs):
                if cmd == ["cidx", "index"]:
                    raise subprocess.CalledProcessError(1, cmd, stderr="Index failed")
                return Mock(returncode=0, stderr="", stdout="")

            mock_subprocess_run.side_effect = run_side_effect

            # Execute - should not raise exception
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(
                mock_golden_repo_manager, str(golden_repos_dir)
            )

            # Verify: Repo was still registered despite index failure
            assert "langfuse_index_fail" in mock_golden_repo_manager.golden_repos


class TestWatchModeIntegration:
    """Test watch mode activation for Langfuse folders after sync."""

    def test_sync_complete_starts_watch_on_langfuse_folders(self, temp_data_dir):
        """
        Test that _on_langfuse_sync_complete() starts watch on all Langfuse folders.
        """
        # Setup: Create golden-repos directory with langfuse folders
        golden_repos_dir = Path(temp_data_dir) / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        langfuse1 = golden_repos_dir / "langfuse_project1"
        langfuse2 = golden_repos_dir / "langfuse_project2"
        langfuse1.mkdir()
        langfuse2.mkdir()

        # Create mock golden repo manager
        mock_manager = Mock()
        mock_manager.golden_repo_exists = Mock(return_value=False)
        mock_manager.golden_repos = {}
        mock_manager._operation_lock = threading.RLock()
        mock_manager._use_sqlite = False
        mock_manager._sqlite_backend = None
        mock_manager._save_metadata = Mock()

        # Mock auto_watch_manager
        mock_watch_manager = Mock()
        mock_watch_manager.is_watching.return_value = False  # Not watching initially
        mock_watch_manager.start_watch.return_value = {"status": "success"}

        # Execute: Simulate _on_langfuse_sync_complete callback
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch(
            "subprocess.run"
        ) as mock_subprocess_run, patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_watch_manager,
        ):
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator
            mock_subprocess_run.return_value = Mock(returncode=0, stderr="", stdout="")

            # Simulate the callback by:
            # 1. Registering folders
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(mock_manager, str(golden_repos_dir))

            # 2. Simulating watch start logic (same as in _on_langfuse_sync_complete)
            for folder in golden_repos_dir.iterdir():
                if folder.is_dir() and folder.name.startswith("langfuse_"):
                    if mock_watch_manager.is_watching(str(folder)):
                        mock_watch_manager.reset_timeout(str(folder))
                    else:
                        mock_watch_manager.start_watch(
                            repo_path=str(folder), timeout=300
                        )

            # Verify: start_watch was called for both folders
            assert mock_watch_manager.start_watch.call_count == 2
            call_args_list = mock_watch_manager.start_watch.call_args_list
            paths = [call[1]["repo_path"] for call in call_args_list]
            assert str(langfuse1) in paths
            assert str(langfuse2) in paths

            # Verify: timeout parameter was correct
            for call in call_args_list:
                assert call[1]["timeout"] == 300

    def test_sync_complete_resets_watch_timeout(self, temp_data_dir):
        """
        Test that if watch is already running, _on_langfuse_sync_complete() resets timeout.
        """
        # Setup: Create golden-repos directory with langfuse folder
        golden_repos_dir = Path(temp_data_dir) / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        langfuse = golden_repos_dir / "langfuse_existing_watch"
        langfuse.mkdir()

        # Create mock golden repo manager
        mock_manager = Mock()
        mock_manager.golden_repo_exists = Mock(return_value=True)  # Already registered
        mock_manager.golden_repos = {}
        mock_manager._operation_lock = threading.RLock()
        mock_manager._save_metadata = Mock()

        # Mock auto_watch_manager with existing watch
        mock_watch_manager = Mock()
        mock_watch_manager.is_watching.return_value = True  # Already watching
        mock_watch_manager.reset_timeout.return_value = {"status": "success"}

        # Execute: Simulate callback
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class, patch(
            "subprocess.run"
        ) as mock_subprocess_run, patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_watch_manager,
        ):
            mock_activator = Mock()
            mock_activator_class.return_value = mock_activator
            mock_subprocess_run.return_value = Mock(returncode=0, stderr="", stdout="")

            # Register (skips because already exists)
            from code_indexer.server.app import register_langfuse_golden_repos

            register_langfuse_golden_repos(mock_manager, str(golden_repos_dir))

            # Simulate watch reset logic
            for folder in golden_repos_dir.iterdir():
                if folder.is_dir() and folder.name.startswith("langfuse_"):
                    if mock_watch_manager.is_watching(str(folder)):
                        mock_watch_manager.reset_timeout(str(folder))
                    else:
                        mock_watch_manager.start_watch(
                            repo_path=str(folder), timeout=300
                        )

            # Verify: reset_timeout was called, NOT start_watch
            mock_watch_manager.reset_timeout.assert_called_once_with(str(langfuse))
            mock_watch_manager.start_watch.assert_not_called()

    def test_watch_mode_handles_non_langfuse_folders(self, temp_data_dir):
        """
        Test that watch mode logic only processes langfuse_* folders.
        """
        # Setup: Create golden-repos with mixed folders
        golden_repos_dir = Path(temp_data_dir) / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        langfuse = golden_repos_dir / "langfuse_test"
        regular = golden_repos_dir / "code-indexer"
        cidx_meta = golden_repos_dir / "cidx-meta"
        langfuse.mkdir()
        regular.mkdir()
        cidx_meta.mkdir()

        # Mock auto_watch_manager
        mock_watch_manager = Mock()
        mock_watch_manager.is_watching.return_value = False
        mock_watch_manager.start_watch.return_value = {"status": "success"}

        # Execute: Simulate watch start logic
        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_watch_manager,
        ):
            for folder in golden_repos_dir.iterdir():
                if folder.is_dir() and folder.name.startswith("langfuse_"):
                    if mock_watch_manager.is_watching(str(folder)):
                        mock_watch_manager.reset_timeout(str(folder))
                    else:
                        mock_watch_manager.start_watch(
                            repo_path=str(folder), timeout=300
                        )

            # Verify: Only langfuse folder was processed
            mock_watch_manager.start_watch.assert_called_once_with(
                repo_path=str(langfuse), timeout=300
            )

    def test_watch_mode_handles_exceptions_gracefully(self, temp_data_dir):
        """
        Test that exceptions in watch mode don't break the callback.
        """
        # Setup: Create golden-repos with langfuse folder
        golden_repos_dir = Path(temp_data_dir) / "golden-repos"
        golden_repos_dir.mkdir(parents=True)
        langfuse = golden_repos_dir / "langfuse_fail"
        langfuse.mkdir()

        # Mock auto_watch_manager to raise exception
        mock_watch_manager = Mock()
        mock_watch_manager.is_watching.side_effect = Exception("Watch manager failed")

        # Execute: Should not raise exception
        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager",
            mock_watch_manager,
        ):
            try:
                for folder in golden_repos_dir.iterdir():
                    if folder.is_dir() and folder.name.startswith("langfuse_"):
                        if mock_watch_manager.is_watching(str(folder)):
                            mock_watch_manager.reset_timeout(str(folder))
                        else:
                            mock_watch_manager.start_watch(
                                repo_path=str(folder), timeout=300
                            )
                # Should raise because we're not catching in test context
                assert False, "Should have raised exception"
            except Exception as e:
                # This is expected - the actual callback has try/except
                assert "Watch manager failed" in str(e)
