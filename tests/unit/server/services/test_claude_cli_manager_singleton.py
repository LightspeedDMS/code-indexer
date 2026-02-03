"""
Tests for ClaudeCliManager singleton pattern (Story #23, AC1).

This module tests:
- Module-level _global_cli_manager variable
- get_claude_cli_manager() function
- initialize_claude_cli_manager() function
- Thread-safe singleton initialization
- update_api_key() method
"""

import threading
from pathlib import Path


class TestClaudeCliManagerSingleton:
    """Tests for AC1: Create Global ClaudeCliManager Singleton with Getter Function."""

    def setup_method(self):
        """Reset singleton state before each test."""
        # Import and reset the global manager to ensure clean state
        from code_indexer.server.services import claude_cli_manager

        # Reset the module-level singleton
        claude_cli_manager._global_cli_manager = None

    def teardown_method(self):
        """Clean up after each test."""
        from code_indexer.server.services import claude_cli_manager

        # Shutdown the manager if it exists
        if claude_cli_manager._global_cli_manager is not None:
            claude_cli_manager._global_cli_manager.shutdown()
            claude_cli_manager._global_cli_manager = None

    def test_get_claude_cli_manager_returns_none_when_not_initialized(self):
        """get_claude_cli_manager() should return None before initialization."""
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
        )

        result = get_claude_cli_manager()
        assert result is None

    def test_initialize_claude_cli_manager_creates_singleton(self, tmp_path: Path):
        """initialize_claude_cli_manager() should create a ClaudeCliManager instance."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
            ClaudeCliManager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = initialize_claude_cli_manager(
            api_key="test-api-key-12345678901234567890",
            meta_dir=meta_dir,
        )

        assert manager is not None
        assert isinstance(manager, ClaudeCliManager)

    def test_initialize_claude_cli_manager_sets_meta_dir(self, tmp_path: Path):
        """initialize_claude_cli_manager() should set _meta_dir on the manager."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = initialize_claude_cli_manager(
            api_key="test-api-key-12345678901234567890",
            meta_dir=meta_dir,
        )

        assert manager._meta_dir == meta_dir

    def test_initialize_claude_cli_manager_sets_api_key(self, tmp_path: Path):
        """initialize_claude_cli_manager() should set _api_key on the manager."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()
        test_key = "sk-ant-test-api-key-12345678901234567890"

        manager = initialize_claude_cli_manager(
            api_key=test_key,
            meta_dir=meta_dir,
        )

        assert manager._api_key == test_key

    def test_initialize_claude_cli_manager_handles_none_api_key(self, tmp_path: Path):
        """initialize_claude_cli_manager() should work with None API key."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = initialize_claude_cli_manager(
            api_key=None,
            meta_dir=meta_dir,
        )

        assert manager is not None
        assert manager._api_key is None

    def test_get_claude_cli_manager_returns_singleton_after_init(self, tmp_path: Path):
        """get_claude_cli_manager() should return the singleton after initialization."""
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        initialized_manager = initialize_claude_cli_manager(
            api_key="test-api-key-12345678901234567890",
            meta_dir=meta_dir,
        )

        retrieved_manager = get_claude_cli_manager()

        assert retrieved_manager is initialized_manager

    def test_multiple_calls_to_initialize_returns_same_instance(self, tmp_path: Path):
        """Multiple calls to initialize_claude_cli_manager() should return the same instance."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager1 = initialize_claude_cli_manager(
            api_key="test-api-key-1",
            meta_dir=meta_dir,
        )
        manager2 = initialize_claude_cli_manager(
            api_key="test-api-key-2",  # Different key should be ignored
            meta_dir=meta_dir,
        )

        assert manager1 is manager2

    def test_thread_safe_singleton_initialization(self, tmp_path: Path):
        """Concurrent initialization should be thread-safe."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        managers: list = []
        errors: list = []

        def initialize_manager():
            try:
                manager = initialize_claude_cli_manager(
                    api_key="test-api-key-12345678901234567890",
                    meta_dir=meta_dir,
                )
                managers.append(manager)
            except Exception as e:
                errors.append(e)

        # Start multiple threads trying to initialize concurrently
        threads = [threading.Thread(target=initialize_manager) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should succeed
        assert len(errors) == 0
        # All should get the same instance
        assert len(set(id(m) for m in managers)) == 1


class TestClaudeCliManagerUpdateApiKey:
    """Tests for update_api_key() method (Story #23, AC3)."""

    def setup_method(self):
        """Reset singleton state before each test."""
        from code_indexer.server.services import claude_cli_manager

        claude_cli_manager._global_cli_manager = None

    def teardown_method(self):
        """Clean up after each test."""
        from code_indexer.server.services import claude_cli_manager

        if claude_cli_manager._global_cli_manager is not None:
            claude_cli_manager._global_cli_manager.shutdown()
            claude_cli_manager._global_cli_manager = None

    def test_update_api_key_updates_manager_key(self, tmp_path: Path):
        """update_api_key() should update the manager's _api_key."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = initialize_claude_cli_manager(
            api_key="old-api-key",
            meta_dir=meta_dir,
        )

        new_key = "new-api-key-12345678901234567890"
        manager.update_api_key(new_key)

        assert manager._api_key == new_key

    def test_update_api_key_with_none_clears_key(self, tmp_path: Path):
        """update_api_key(None) should clear the API key."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        meta_dir = tmp_path / "cidx-meta"
        meta_dir.mkdir()

        manager = initialize_claude_cli_manager(
            api_key="existing-api-key",
            meta_dir=meta_dir,
        )

        manager.update_api_key(None)

        assert manager._api_key is None


class TestModuleLevelExports:
    """Tests that required functions are exported in module's public API."""

    def test_get_claude_cli_manager_is_exported(self):
        """get_claude_cli_manager should be importable from the module."""
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
        )

        assert callable(get_claude_cli_manager)

    def test_initialize_claude_cli_manager_is_exported(self):
        """initialize_claude_cli_manager should be importable from the module."""
        from code_indexer.server.services.claude_cli_manager import (
            initialize_claude_cli_manager,
        )

        assert callable(initialize_claude_cli_manager)

    def test_global_cli_manager_variable_exists(self):
        """Module should have _global_cli_manager variable."""
        from code_indexer.server.services import claude_cli_manager

        assert hasattr(claude_cli_manager, "_global_cli_manager")
