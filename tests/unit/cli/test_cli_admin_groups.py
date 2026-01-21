"""
Tests for CLI admin groups commands - Story #747 requirements.

This test file validates:
1. All 9 admin groups commands exist
2. All commands support --json output flag
3. Delete command requires --confirm flag for safety
4. Help text contains required information

Following TDD methodology - these tests define the expected behavior.
"""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestAdminGroupsCommandStructure:
    """Test that all 9 admin groups commands exist and are properly structured."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_group_exists(self, mock_detect_mode, runner: CliRunner):
        """Test that admin groups command group exists."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "groups" in result.output.lower() or "group" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_nine_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 9 admin groups commands exist."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        required_commands = [
            "list",
            "create",
            "show",
            "update",
            "delete",
            "add-member",
            "add-repos",
            "remove-repo",
            "remove-repos",
        ]
        for cmd in required_commands:
            assert cmd in result.output, f"Command '{cmd}' not found in admin groups"


class TestAdminGroupsJsonOutput:
    """Test --json output flag for all admin groups commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_list_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin groups list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_create_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin groups create command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_show_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin groups show command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "show", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_update_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin groups update command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "update", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_delete_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin groups delete command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output


class TestAdminGroupsDeleteConfirmFlag:
    """Test --confirm flag requirement for delete command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_delete_has_confirm_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin groups delete command has --confirm flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--confirm" in result.output


class TestAdminGroupsHelpTexts:
    """Test help text requirements from Story #747."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_create_help_has_name_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin groups create help shows --name option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--name" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_add_member_help_has_user_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin groups add-member help shows --user option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "add-member", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--user" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_groups_add_repos_help_has_repos_option(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin groups add-repos help shows --repos option."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "groups", "add-repos", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--repos" in result.output
