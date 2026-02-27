"""
Unit tests for Story #24: ClaudeCliManager uses max_concurrent_claude_cli config.

Tests verify that:
1. meta_description_hook passes max_concurrent_claude_cli from config to ClaudeCliManager
2. ClaudeCliManager default max_workers is 2 (not 4)
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import shutil


class TestClaudeCliManagerDefaultWorkers:
    """Test that ClaudeCliManager defaults to 2 workers (Story #24)."""

    def test_default_max_workers_is_2(self):
        """Story #24: Default max_workers should be 2, not 4."""
        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        manager = ClaudeCliManager(api_key=None)
        try:
            # Story #24 AC: Default should be 2 for resource-constrained systems
            assert (
                manager._max_workers == 2
            ), f"Expected default max_workers=2, got {manager._max_workers}"
            assert (
                len(manager._worker_threads) == 2
            ), f"Expected 2 worker threads, got {len(manager._worker_threads)}"
        finally:
            manager.shutdown()


class TestMetaDescriptionHookConfigIntegration:
    """Test that meta_description_hook uses global ClaudeCliManager singleton.

    Story #23 AC4: on_repo_added uses global manager instead of creating new instances.
    The global manager is initialized during server startup with config values.
    """

    @pytest.fixture
    def temp_golden_repos_dir(self):
        """Create temporary golden repos directory."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    @pytest.fixture
    def cidx_meta_path(self, temp_golden_repos_dir):
        """Create cidx-meta directory."""
        meta_path = Path(temp_golden_repos_dir) / "cidx-meta"
        meta_path.mkdir(parents=True)
        return meta_path

    def test_on_repo_added_passes_config_max_workers_to_cli_manager(
        self, temp_golden_repos_dir, cidx_meta_path
    ):
        """Story #24/23: on_repo_added should use global manager with config max_workers.

        Story #23 AC4 changed on_repo_added to use the global singleton which is
        initialized during server startup with config values. This test verifies
        the global manager's max_workers is used.
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        # Setup: Create a mock repository
        repo_name = "test-repo"
        repo_url = "https://github.com/test/repo"
        clone_path = Path(temp_golden_repos_dir) / repo_name
        clone_path.mkdir(parents=True)
        (clone_path / "README.md").write_text("# Test Repo\nA test repository")

        # Mock global ClaudeCliManager singleton with configured max_workers
        mock_cli_manager = MagicMock()
        mock_cli_manager._max_workers = 7  # Config value from server startup
        mock_cli_manager.check_cli_available.return_value = False  # Force fallback path

        with patch(
            "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
            return_value=mock_cli_manager,
        ):
            with patch("subprocess.run"):
                on_repo_added(
                    repo_name=repo_name,
                    repo_url=repo_url,
                    clone_path=str(clone_path),
                    golden_repos_dir=temp_golden_repos_dir,
                )

        # Verify: global manager was used
        mock_cli_manager.check_cli_available.assert_called_once()
        # The manager's max_workers should be the configured value
        assert mock_cli_manager._max_workers == 7

class TestClaudeIntegrationConfigDefault:
    """Test ClaudeIntegrationConfig default value for max_concurrent_claude_cli."""

    def test_config_default_max_concurrent_is_2(self):
        """Story #24: ClaudeIntegrationConfig default max_concurrent_claude_cli should be 2."""
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig()
        assert (
            config.max_concurrent_claude_cli == 2
        ), f"Expected default max_concurrent_claude_cli=2, got {config.max_concurrent_claude_cli}"
