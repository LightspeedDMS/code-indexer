"""
Tests for CLI admin api-keys commands - Story #748 requirements.

This test file validates:
1. All 3 admin api-keys commands exist
2. All commands support --json output flag
3. Delete command requires --confirm flag for safety
4. Help text contains required information

Following TDD methodology - these tests define the expected behavior.
"""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestAdminAPIKeysCommandStructure:
    """Test that all 3 admin api-keys commands exist and are properly structured."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_group_exists(self, mock_detect_mode, runner: CliRunner):
        """Test that admin api-keys command group exists."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "api" in result.output.lower() or "key" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_three_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 3 admin api-keys commands exist."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        required_commands = [
            "list",
            "create",
            "delete",
        ]
        for cmd in required_commands:
            assert cmd in result.output, f"Command '{cmd}' not found in admin api-keys"


class TestAdminAPIKeysJsonOutput:
    """Test --json output flag for all admin api-keys commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_list_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin api-keys list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_create_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin api-keys create command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_delete_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin api-keys delete command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output


class TestAdminAPIKeysDeleteConfirmFlag:
    """Test --confirm flag requirement for delete command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_delete_has_confirm_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin api-keys delete command has --confirm flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--confirm" in result.output


class TestAdminAPIKeysHelpTexts:
    """Test help text requirements from Story #748."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_create_help_has_description_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin api-keys create help shows --description option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--description" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_api_keys_delete_help_shows_key_id_argument(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin api-keys delete help shows KEY_ID argument."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "api-keys", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check for KEY_ID argument (case-insensitive)
        assert "key_id" in result.output.lower() or "key-id" in result.output.lower()
