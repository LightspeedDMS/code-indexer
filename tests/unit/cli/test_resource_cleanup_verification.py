"""Test to verify API client resource cleanup fix."""

import pytest
from unittest.mock import Mock, patch
from click.testing import CliRunner
from pathlib import Path

# Import CLI components
from code_indexer.cli import cli
from code_indexer.api_clients.repos_client import ActivatedRepository


class TestResourceCleanupFix:
    """Test class for verifying resource cleanup fix."""

    @pytest.fixture(autouse=True)
    def mock_remote_mode_gate(self):
        """Auto-mock the require_mode("remote") gate on repos_group/admin_group.

        disabled_commands.py independently binds detect_current_mode via its
        own module-level import, so it is unaffected by patching
        find_project_root at code_indexer.mode_detection.command_mode_detector
        (see #1742). Without this, every gated group invocation raises
        DisabledCommandError before the mocked API clients are ever reached.
        """
        with patch(
            "code_indexer.disabled_commands.detect_current_mode",
            return_value="remote",
        ):
            yield

    def setup_method(self):
        """Setup test environment for each test."""
        self.runner = CliRunner()

    @patch("code_indexer.remote.sync_execution._load_remote_configuration")
    @patch("code_indexer.remote.sync_execution._load_and_decrypt_credentials")
    @patch("code_indexer.mode_detection.command_mode_detector.find_project_root")
    @patch("code_indexer.cli.ReposAPIClient")
    def test_repos_list_client_resource_cleanup(
        self,
        mock_repos_client_class,
        mock_find_project_root,
        mock_load_credentials,
        mock_load_config,
    ):
        """Test that repos list properly closes API client after use."""
        # Setup mocks for CLI prerequisites
        mock_find_project_root.return_value = Path("/fake/project")
        mock_load_config.return_value = {"server_url": "http://localhost:8000"}
        mock_load_credentials.return_value = {
            "username": "test",
            "password": "fake_password",
            "access_token": "fake_token",
        }

        # Create mock ReposAPIClient instance
        mock_client = Mock()
        mock_repos_client_class.return_value = mock_client

        # Mock the client methods (all synchronous on the real ReposAPIClient)
        mock_client.list_activated_repositories = Mock(
            return_value=[
                ActivatedRepository(
                    alias="my-project",
                    current_branch="main",
                    sync_status="synced",
                    last_sync="2024-01-16T08:15:00Z",
                    activation_date="2024-01-15T10:30:00Z",
                    conflict_details=None,
                )
            ]
        )
        mock_client.close = Mock()

        # Execute the command
        result = self.runner.invoke(cli, ["repos", "list"])

        # Should succeed
        assert result.exit_code == 0, f"Command should succeed, got: {result.output}"
        assert "my-project" in result.output

        # CRITICAL: Verify that client.close() was called for proper resource cleanup
        mock_client.close.assert_called_once()

    @patch("code_indexer.mode_detection.command_mode_detector.find_project_root")
    @patch("code_indexer.remote.config.load_remote_configuration")
    @patch("code_indexer.remote.credential_manager.ProjectCredentialManager")
    @patch("code_indexer.remote.credential_manager.load_encrypted_credentials")
    @patch("code_indexer.api_clients.admin_client.AdminAPIClient")
    def test_admin_repos_list_client_resource_cleanup(
        self,
        mock_admin_client_class,
        mock_load_credentials,
        mock_credential_manager_class,
        mock_load_config,
        mock_find_project_root,
    ):
        """Test that admin repos list properly closes API client after use."""
        # Setup basic mocks
        mock_find_project_root.return_value = Path("/fake/project")
        mock_load_config.return_value = {
            "server_url": "http://localhost:8000",
            "username": "admin",
        }

        # Create credential manager mock
        mock_credential_manager = Mock()
        mock_credential_manager_class.return_value = mock_credential_manager
        mock_load_credentials.return_value = {
            "username": "admin",
            "password": "admin_password",
            "access_token": "fake-admin-token",
        }

        # Mock AdminAPIClient
        mock_admin_client = Mock()
        mock_admin_client.close = Mock()
        mock_admin_client_class.return_value = mock_admin_client

        # Mock successful response
        mock_admin_client.list_golden_repositories.return_value = {
            "golden_repositories": [
                {
                    "alias": "test-repo",
                    "repo_url": "https://github.com/test/repo.git",
                    "last_refresh": "2024-01-16T10:00:00Z",
                    "status": "ready",
                }
            ],
            "total": 1,
        }

        # Execute command
        result = self.runner.invoke(cli, ["admin", "repos", "list"])

        # Should succeed
        assert result.exit_code == 0, (
            f"Admin repos list should succeed, got: {result.output}"
        )
        assert "test-repo" in result.output

        # CRITICAL: Verify that client.close() was called for proper resource cleanup.
        # Note: cli.py's admin_repos_list genuinely calls close() twice (inner
        # finally in fetch_admin_data() + outer finally) -- idempotent in the
        # real client, so we assert it was called rather than an exact count.
        mock_admin_client.close.assert_called()
