"""
Unit tests for GoldenRepoManager.register_local_repo() method.

Tests the synchronous local (non-git) repository registration feature
that consolidates three separate registration patterns in app.py.
"""

import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import threading
import time

import pytest

from src.code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepo,
)
from src.code_indexer.server.repositories.background_jobs import BackgroundJobManager


class TestRegisterLocalRepo:
    """Unit tests for register_local_repo() method."""

    @pytest.fixture
    def temp_data_dir(self):
        """Create temporary data directory for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def golden_repo_manager(self, temp_data_dir):
        """Create GoldenRepoManager instance with temp directory."""
        manager = GoldenRepoManager(data_dir=temp_data_dir)
        # Inject mock BackgroundJobManager
        mock_bg_manager = MagicMock(spec=BackgroundJobManager)
        mock_bg_manager.submit_job.return_value = "test-job-id-12345"
        manager.background_job_manager = mock_bg_manager
        return manager

    @pytest.fixture
    def test_folder(self, temp_data_dir):
        """Create a test folder to register."""
        folder_path = Path(temp_data_dir) / "test-local-repo"
        folder_path.mkdir(parents=True, exist_ok=True)
        return folder_path

    def test_register_local_repo_new_registration_returns_true(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo returns True for new registration.

        AC1: New registration returns True
        """
        result = golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )

        assert result is True, "New registration should return True"

    def test_register_local_repo_duplicate_registration_returns_false(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo returns False for duplicate registration (idempotent).

        AC6: Duplicate registration returns False
        """
        # First registration
        result1 = golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )
        assert result1 is True, "First registration should return True"

        # Second registration (duplicate)
        result2 = golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )
        assert result2 is False, "Duplicate registration should return False (idempotent)"

    def test_register_local_repo_validates_alias_path_traversal(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo rejects aliases with path traversal characters.

        AC1: Validate alias (reuse existing path traversal checks)
        """
        dangerous_aliases = ["../escape", "foo/bar", "foo\\bar", "../../etc/passwd"]

        for alias in dangerous_aliases:
            with pytest.raises(
                ValueError,
                match="Invalid alias.*cannot contain path traversal characters",
            ):
                golden_repo_manager.register_local_repo(
                    alias=alias,
                    folder_path=test_folder,
                    fire_lifecycle_hooks=False,
                )

    def test_register_local_repo_creates_golden_repo_record(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo creates GoldenRepo record with correct attributes.

        AC1: Create GoldenRepo record with repo_url="local://{alias}"
        """
        golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )

        # Verify repo exists in golden_repos dict
        assert "test-repo" in golden_repo_manager.golden_repos
        repo = golden_repo_manager.golden_repos["test-repo"]

        # Verify repo attributes
        assert repo.alias == "test-repo"
        assert repo.repo_url == "local://test-repo"
        assert repo.clone_path == str(test_folder)
        assert repo.enable_temporal is False
        assert repo.temporal_options is None

    def test_register_local_repo_persists_to_sqlite_backend(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo persists to SQLite backend.

        AC1: Persist to storage backend (SQLite or JSON)
        """
        # Mock SQLite backend (always active)
        mock_sqlite = MagicMock()
        golden_repo_manager._sqlite_backend = mock_sqlite

        golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )

        # Verify SQLite backend was called
        mock_sqlite.add_repo.assert_called_once()
        call_kwargs = mock_sqlite.add_repo.call_args.kwargs
        assert call_kwargs["alias"] == "test-repo"
        assert call_kwargs["repo_url"] == "local://test-repo"
        assert call_kwargs["clone_path"] == str(test_folder)
        assert call_kwargs["enable_temporal"] is False

    def test_register_local_repo_persists_to_json_when_no_sqlite(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo persists to JSON when SQLite not available.

        AC1: Persist to storage backend (SQLite)
        """
        # Mock SQLite backend (always active)
        mock_sqlite = MagicMock()
        golden_repo_manager._sqlite_backend = mock_sqlite

        golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )

        # Verify SQLite backend add_repo was called
        mock_sqlite.add_repo.assert_called_once()

    def test_register_local_repo_calls_global_activator(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo calls GlobalActivator.activate_golden_repo().

        AC1: Call GlobalActivator.activate_golden_repo()
        """
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = MagicMock()
            mock_activator_class.return_value = mock_activator

            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

            # Verify GlobalActivator was instantiated with golden_repos_dir
            mock_activator_class.assert_called_once_with(
                golden_repo_manager.golden_repos_dir
            )

            # Verify activate_golden_repo was called
            mock_activator.activate_golden_repo.assert_called_once_with(
                repo_name="test-repo",
                repo_url="local://test-repo",
                clone_path=str(test_folder),
                enable_temporal=False,
                temporal_options=None,
            )

    def test_register_local_repo_fires_lifecycle_hooks_when_enabled(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo fires lifecycle hooks when fire_lifecycle_hooks=True.

        AC6: Fires lifecycle hooks when fire_lifecycle_hooks=True
        """
        # Mock group_access_manager
        mock_group_manager = MagicMock()
        golden_repo_manager.group_access_manager = mock_group_manager

        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added"
        ) as mock_meta_hook, patch(
            "code_indexer.server.services.group_access_hooks.on_repo_added"
        ) as mock_group_hook:
            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=True,
            )

            # Verify meta description hook was called
            mock_meta_hook.assert_called_once_with(
                repo_name="test-repo",
                repo_url="local://test-repo",
                clone_path=str(test_folder),
                golden_repos_dir=golden_repo_manager.golden_repos_dir,
            )

            # Verify group access hook was called
            mock_group_hook.assert_called_once_with("test-repo", mock_group_manager)

    def test_register_local_repo_skips_lifecycle_hooks_when_disabled(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo skips lifecycle hooks when fire_lifecycle_hooks=False.

        AC6: Skips lifecycle hooks when fire_lifecycle_hooks=False
        """
        # Mock group_access_manager
        mock_group_manager = MagicMock()
        golden_repo_manager.group_access_manager = mock_group_manager

        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added"
        ) as mock_meta_hook, patch(
            "code_indexer.server.services.group_access_hooks.on_repo_added"
        ) as mock_group_hook:
            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

            # Verify hooks were NOT called
            mock_meta_hook.assert_not_called()
            mock_group_hook.assert_not_called()

    def test_register_local_repo_skips_group_hook_when_manager_none(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo skips group access hook when group_access_manager is None.

        AC6: Handle case where group_access_manager is not initialized
        """
        # Ensure group_access_manager is None
        golden_repo_manager.group_access_manager = None

        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added"
        ) as mock_meta_hook, patch(
            "code_indexer.server.services.group_access_hooks.on_repo_added"
        ) as mock_group_hook:
            golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=True,
            )

            # Verify meta hook was called
            mock_meta_hook.assert_called_once()

            # Verify group hook was NOT called (manager is None)
            mock_group_hook.assert_not_called()

    def test_register_local_repo_thread_safety_concurrent_calls(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo is thread-safe with concurrent calls.

        AC6: Thread safety with concurrent calls
        """
        results = []
        errors = []

        def register_repo(alias):
            try:
                result = golden_repo_manager.register_local_repo(
                    alias=alias,
                    folder_path=test_folder,
                    fire_lifecycle_hooks=False,
                )
                results.append((alias, result))
            except Exception as e:
                errors.append((alias, e))

        # Create 10 threads trying to register the same alias
        threads = []
        for i in range(10):
            thread = threading.Thread(target=register_repo, args=("test-repo",))
            threads.append(thread)

        # Start all threads
        for thread in threads:
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Verify no errors occurred
        assert len(errors) == 0, f"Unexpected errors: {errors}"

        # Verify exactly one thread got True (new registration)
        # and the rest got False (duplicate)
        true_count = sum(1 for _, result in results if result is True)
        false_count = sum(1 for _, result in results if result is False)

        assert true_count == 1, "Exactly one thread should get True (new registration)"
        assert false_count == 9, "Nine threads should get False (duplicate)"

        # Verify repo exists in golden_repos dict
        assert "test-repo" in golden_repo_manager.golden_repos

    def test_register_local_repo_thread_safety_uses_operation_lock(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo uses _operation_lock for thread safety.

        AC1: Use self._operation_lock for thread safety
        """
        # Replace lock with a mock that wraps the real lock
        real_lock = golden_repo_manager._operation_lock
        mock_lock = MagicMock(wraps=real_lock)
        golden_repo_manager._operation_lock = mock_lock

        golden_repo_manager.register_local_repo(
            alias="test-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )

        # Verify lock was used as context manager (__enter__ called)
        mock_lock.__enter__.assert_called()

    def test_register_local_repo_graceful_global_activation_failure(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo handles GlobalActivator failures gracefully.

        AC1: Non-blocking post-registration step (logs error but doesn't fail)
        """
        with patch(
            "code_indexer.global_repos.global_activation.GlobalActivator"
        ) as mock_activator_class:
            mock_activator = MagicMock()
            mock_activator.activate_golden_repo.side_effect = Exception(
                "Activation failed"
            )
            mock_activator_class.return_value = mock_activator

            # Should not raise exception
            result = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=False,
            )

            # Verify registration succeeded despite activation failure
            assert result is True
            assert "test-repo" in golden_repo_manager.golden_repos

    def test_register_local_repo_graceful_lifecycle_hook_failures(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that register_local_repo handles lifecycle hook failures gracefully.

        AC6: Lifecycle hooks log errors but don't fail the registration
        """
        # Mock group_access_manager
        mock_group_manager = MagicMock()
        golden_repo_manager.group_access_manager = mock_group_manager

        with patch(
            "code_indexer.global_repos.meta_description_hook.on_repo_added"
        ) as mock_meta_hook, patch(
            "code_indexer.server.services.group_access_hooks.on_repo_added"
        ) as mock_group_hook:
            # Make hooks raise exceptions
            mock_meta_hook.side_effect = Exception("Meta hook failed")
            mock_group_hook.side_effect = Exception("Group hook failed")

            # Should not raise exception
            result = golden_repo_manager.register_local_repo(
                alias="test-repo",
                folder_path=test_folder,
                fire_lifecycle_hooks=True,
            )

            # Verify registration succeeded despite hook failures
            assert result is True
            assert "test-repo" in golden_repo_manager.golden_repos


class TestRegisterLocalRepoIntegration:
    """Integration tests for register_local_repo() with real components."""

    @pytest.fixture
    def temp_data_dir(self):
        """Create temporary data directory for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def golden_repo_manager(self, temp_data_dir):
        """Create GoldenRepoManager instance with real storage backend."""
        manager = GoldenRepoManager(data_dir=temp_data_dir)
        # Inject mock BackgroundJobManager
        mock_bg_manager = MagicMock(spec=BackgroundJobManager)
        mock_bg_manager.submit_job.return_value = "test-job-id-12345"
        manager.background_job_manager = mock_bg_manager
        return manager

    @pytest.fixture
    def test_folder(self, temp_data_dir):
        """Create a test folder to register."""
        folder_path = Path(temp_data_dir) / "golden-repos" / "test-local-repo"
        folder_path.mkdir(parents=True, exist_ok=True)
        return folder_path

    def test_register_local_repo_appears_in_list_golden_repos(
        self, golden_repo_manager, test_folder
    ):
        """
        Test that repo registered via register_local_repo appears in list_golden_repos().

        AC6: Integration test - Langfuse folder registered via register_local_repo()
        appears in list_golden_repos()
        """
        # Register local repo
        result = golden_repo_manager.register_local_repo(
            alias="test-local-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )
        assert result is True

        # Verify repo appears in list_golden_repos
        repos = golden_repo_manager.list_golden_repos()
        repo_aliases = [repo["alias"] for repo in repos]

        assert "test-local-repo" in repo_aliases
        # Find the repo and verify its attributes
        repo = next((r for r in repos if r["alias"] == "test-local-repo"), None)
        assert repo is not None
        assert repo["repo_url"] == "local://test-local-repo"
        assert repo["clone_path"] == str(test_folder)

    def test_register_local_repo_persists_across_manager_instances(
        self, temp_data_dir, test_folder
    ):
        """
        Test that repo registered via register_local_repo persists across manager instances.

        Verifies that storage persistence (SQLite or JSON) works correctly.
        """
        # Create first manager instance and register repo
        manager1 = GoldenRepoManager(data_dir=temp_data_dir)
        mock_bg_manager = MagicMock(spec=BackgroundJobManager)
        manager1.background_job_manager = mock_bg_manager

        result = manager1.register_local_repo(
            alias="test-local-repo",
            folder_path=test_folder,
            fire_lifecycle_hooks=False,
        )
        assert result is True

        # Create second manager instance (should load persisted data)
        manager2 = GoldenRepoManager(data_dir=temp_data_dir)
        manager2.background_job_manager = mock_bg_manager

        # Verify repo exists in second instance
        assert manager2.golden_repo_exists("test-local-repo")
        repo = manager2.get_golden_repo("test-local-repo")
        assert repo is not None
        assert repo.alias == "test-local-repo"
        assert repo.repo_url == "local://test-local-repo"
