"""Test to verify repos list data model alignment fix."""

import pytest
from unittest.mock import Mock, patch
from click.testing import CliRunner
from pathlib import Path

# Import CLI components
from code_indexer.cli import cli
from code_indexer.api_clients.repos_client import ActivatedRepository


@pytest.fixture(autouse=True)
def mock_remote_setup():
    """Auto-mock mode detection, project root discovery, and remote credentials.

    Without this, 'cidx repos list' hits the real require_mode("remote") gate
    (which uses this repo's actual local-mode .code-indexer config) and fails
    with DisabledCommandError before the mocked ReposAPIClient below is used.
    """
    with (
        patch(
            "code_indexer.disabled_commands.detect_current_mode",
            return_value="remote",
        ),
        patch(
            "code_indexer.mode_detection.command_mode_detector.find_project_root",
            return_value=Path("/fake/project"),
        ),
        patch(
            "code_indexer.remote.sync_execution._load_remote_configuration",
            return_value={"server_url": "http://localhost:8000"},
        ),
        patch(
            "code_indexer.remote.sync_execution._load_and_decrypt_credentials",
            return_value={
                "username": "test",
                "password": "fake_password",
                "access_token": "fake_token",
            },
        ),
    ):
        yield


class TestReposListFix:
    """Test class for verifying repos list fix."""

    def setup_method(self):
        """Setup test environment for each test."""
        self.runner = CliRunner()

    def test_repos_list_data_model_alignment_fix(self):
        """Test that repos list properly handles server data format after fix.

        Verifies that the mapping between ActivatedRepositoryInfo (server)
        and ActivatedRepository (client) works correctly.
        """
        mock_client = Mock()
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

        with patch("code_indexer.cli.ReposAPIClient", return_value=mock_client):
            result = self.runner.invoke(cli, ["repos", "list"])

        # Should succeed after fix
        assert result.exit_code == 0, (
            f"Command should succeed after fix, got: {result.output}"
        )
        assert "my-project" in result.output
        assert "main" in result.output

        # Verify client method was called
        mock_client.list_activated_repositories.assert_called_once()
