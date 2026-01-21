"""
Tests for CLI admin mcp-credentials commands - Story #748 requirements.

This test file validates:
1. All 7 admin mcp-credentials commands exist
2. All commands support --json output flag
3. Delete commands require --confirm flag for safety
4. Admin-only commands exist with correct parameters
5. Help text contains required information

Following TDD methodology - these tests define the expected behavior.
"""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestAdminMCPCredentialsCommandStructure:
    """Test that all 7 admin mcp-credentials commands exist and are properly structured."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_group_exists(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials command group exists."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "mcp" in result.output.lower() or "credential" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_seven_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 7 admin mcp-credentials commands exist."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        # User self-service commands (3)
        required_commands = [
            "list",
            "create",
            "delete",
            # Admin-only commands (4)
            "list-user",
            "create-for-user",
            "delete-for-user",
            "list-all",
        ]
        for cmd in required_commands:
            assert (
                cmd in result.output
            ), f"Command '{cmd}' not found in admin mcp-credentials"


class TestAdminMCPCredentialsSelfServiceJsonOutput:
    """Test --json output flag for self-service mcp-credentials commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_list_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_create_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials create command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials delete command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output


class TestAdminMCPCredentialsAdminOnlyJsonOutput:
    """Test --json output flag for admin-only mcp-credentials commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_list_user_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials list-user command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "list-user", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_create_for_user_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials create-for-user command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "create-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_for_user_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials delete-for-user command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "delete-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_list_all_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials list-all command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "list-all", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output


class TestAdminMCPCredentialsDeleteConfirmFlags:
    """Test --confirm flag requirement for delete commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_has_confirm_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials delete command has --confirm flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--confirm" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_for_user_has_confirm_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin mcp-credentials delete-for-user command has --confirm flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "delete-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--confirm" in result.output


class TestAdminMCPCredentialsHelpTexts:
    """Test help text requirements from Story #748."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_create_help_has_description_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials create help shows --description option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--description" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_help_shows_cred_id_argument(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials delete help shows CRED_ID argument."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check for CRED_ID argument (case-insensitive)
        assert "cred_id" in result.output.lower() or "cred-id" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_list_user_help_shows_username_argument(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials list-user help shows USERNAME argument."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "mcp-credentials", "list-user", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "username" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_create_for_user_help_has_description_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials create-for-user help shows --description option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "create-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--description" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_create_for_user_help_shows_username_argument(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials create-for-user help shows USERNAME argument."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "create-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "username" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_mcp_credentials_delete_for_user_help_shows_username_and_cred_id(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin mcp-credentials delete-for-user help shows USERNAME and CRED_ID."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(
            cli, ["admin", "mcp-credentials", "delete-for-user", "--help"]
        )
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "username" in result.output.lower()
        assert "cred_id" in result.output.lower() or "cred-id" in result.output.lower()
