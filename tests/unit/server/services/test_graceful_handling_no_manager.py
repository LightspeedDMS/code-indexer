"""
Tests for handling when ClaudeCliManager is not initialized (Story #23, AC5).

This module tests:
- All components that use get_claude_cli_manager() handle None appropriately
- on_repo_added raises RuntimeError (anti-fallback contract) when manager is not initialized
- trigger_catchup returns False gracefully when manager is not available
- Appropriate logging occurs when manager is not available
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestGracefulHandlingWhenManagerNotInitialized:
    """Tests for AC5: Graceful Handling When Manager Not Initialized."""

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

    def test_get_claude_cli_manager_returns_none_not_exception(self):
        """get_claude_cli_manager() should return None, not raise exception."""
        from code_indexer.server.services.claude_cli_manager import (
            get_claude_cli_manager,
        )

        # Should return None without raising
        result = get_claude_cli_manager()
        assert result is None

    def test_trigger_catchup_graceful_when_no_manager(self, caplog):
        """trigger_catchup_on_api_key_save should return False gracefully, not raise."""
        from code_indexer.server.routers.api_keys import trigger_catchup_on_api_key_save

        # Should not raise, should return False
        with caplog.at_level(logging.WARNING):
            result = trigger_catchup_on_api_key_save("placeholder-not-a-key")

        assert result is False
        assert any(
            "not initialized" in record.message.lower() for record in caplog.records
        )

    def test_on_repo_added_raises_when_no_manager(self, tmp_path: Path):
        """on_repo_added raises RuntimeError when manager not initialized (anti-fallback contract)."""
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        # Setup directory structure
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()
        cidx_meta_dir = golden_repos_dir / "cidx-meta"
        cidx_meta_dir.mkdir()

        # Create test repo with README
        test_repo_dir = tmp_path / "test-repo"
        test_repo_dir.mkdir()
        readme = test_repo_dir / "README.md"
        readme.write_text("# Test Repo\n\nA test repository.")

        # v10.4.13 anti-fallback contract: must raise RuntimeError when manager is None
        with pytest.raises(RuntimeError, match="anti-fallback"):
            on_repo_added(
                repo_name="test-repo",
                repo_url="https://github.com/test/test-repo",
                clone_path=str(test_repo_dir),
                golden_repos_dir=str(golden_repos_dir),
            )

    def test_on_repo_added_raises_when_no_readme_and_no_manager(self, tmp_path: Path):
        """on_repo_added raises RuntimeError when no manager, regardless of README presence."""
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        # Setup directory structure
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()
        (golden_repos_dir / "cidx-meta").mkdir()

        # Create test repo WITHOUT README
        test_repo_dir = tmp_path / "test-repo"
        test_repo_dir.mkdir()

        # v10.4.13 anti-fallback contract: RuntimeError raised before README check
        with pytest.raises(RuntimeError, match="anti-fallback"):
            on_repo_added(
                repo_name="test-repo",
                repo_url="https://github.com/test/test-repo",
                clone_path=str(test_repo_dir),
                golden_repos_dir=str(golden_repos_dir),
            )

    def test_server_startup_graceful_when_initialization_fails(
        self, tmp_path: Path, caplog
    ):
        """Server startup should not crash if ClaudeCliManager initialization fails."""
        from code_indexer.server.startup.claude_cli_startup import (
            initialize_claude_manager_on_startup,
        )

        # Use invalid path that can't be created
        mock_config = MagicMock()
        mock_config.claude_integration_config.anthropic_api_key = None
        mock_config.claude_integration_config.max_concurrent_claude_cli = 2

        with caplog.at_level(logging.ERROR):
            result = initialize_claude_manager_on_startup(
                golden_repos_dir="/invalid/path/that/cannot/be/created",
                server_config=mock_config,
            )

        # Should return False, not raise
        assert result is False
        assert any("failed" in record.message.lower() for record in caplog.records)


class TestGracefulHandlingLogging:
    """Tests for proper logging when manager is not available."""

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

    def test_trigger_catchup_logs_warning_when_no_manager(self, caplog):
        """trigger_catchup should log WARNING when manager not initialized."""
        from code_indexer.server.routers.api_keys import trigger_catchup_on_api_key_save

        with caplog.at_level(logging.WARNING):
            trigger_catchup_on_api_key_save("placeholder-not-a-key")

        # Should have WARNING level log about manager not initialized
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warning_records) > 0
        assert any(
            "ClaudeCliManager" in r.message and "not initialized" in r.message
            for r in warning_records
        )

    def test_on_repo_added_raises_when_no_manager(self, tmp_path: Path):
        """on_repo_added raises RuntimeError (anti-fallback contract) when manager not initialized."""
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        # Setup
        golden_repos_dir = tmp_path / "golden-repos"
        golden_repos_dir.mkdir()
        (golden_repos_dir / "cidx-meta").mkdir()
        test_repo_dir = tmp_path / "test-repo"
        test_repo_dir.mkdir()
        (test_repo_dir / "README.md").write_text("# Test")

        # v10.4.13 anti-fallback contract: no fallback logging — RuntimeError raised instead
        with pytest.raises(RuntimeError, match="anti-fallback"):
            on_repo_added(
                repo_name="test-repo",
                repo_url="https://github.com/test/repo",
                clone_path=str(test_repo_dir),
                golden_repos_dir=str(golden_repos_dir),
            )
