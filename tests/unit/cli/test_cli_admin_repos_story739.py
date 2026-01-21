"""
Tests for CLI admin repos commands - Story #739 requirements.

This test file validates:
1. All 6 admin repos commands support --json output flag
2. Delete command requires --confirm flag for safety
3. Help text contains required information

Following TDD methodology - these tests define the expected behavior.
"""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestAdminReposJsonOutput:
    """Test --json output flag for all admin repos commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_add_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin repos add command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "add", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_list_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin repos list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_show_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin repos show command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "show", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_refresh_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin repos refresh command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "refresh", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_branches_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin repos branches command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "branches", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_delete_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin repos delete command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output


class TestAdminReposDeleteConfirmFlag:
    """Test --confirm flag requirement for delete command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_delete_has_confirm_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin repos delete command has --confirm flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--confirm" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_delete_confirm_flag_description(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that --confirm flag has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check that confirm flag is described as required for deletion
        help_lower = result.output.lower()
        assert "confirm" in help_lower
        assert "required" in help_lower or "confirm deletion" in help_lower


class TestAdminReposHelpTexts:
    """Test help text requirements from Story #739."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_add_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos add help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "add", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check required flags mentioned in spec
        assert "--url" in result.output or "GIT_URL" in result.output
        assert "--alias" in result.output or "ALIAS" in result.output
        assert "--branch" in result.output or "--default-branch" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_list_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos list help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "golden repositories" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_show_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos show help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "show", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "ALIAS" in result.output
        assert (
            "details" in result.output.lower() or "information" in result.output.lower()
        )

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_refresh_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos refresh help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "refresh", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "ALIAS" in result.output
        assert "refresh" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_branches_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos branches help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "branches", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "ALIAS" in result.output
        assert "branch" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_delete_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos delete help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "ALIAS" in result.output
        assert "--confirm" in result.output


class TestAdminReposCommandStructure:
    """Test that all 6 admin repos commands exist and are properly structured."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_six_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 6 admin repos commands exist."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        required_commands = ["add", "list", "show", "refresh", "branches", "delete"]
        for cmd in required_commands:
            assert (
                cmd in result.output
            ), f"Command '{cmd}' not found in admin repos group"

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_repos_group_description(self, mock_detect_mode, runner: CliRunner):
        """Test admin repos group has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "repos", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        help_lower = result.output.lower()
        assert "repository" in help_lower
        assert (
            "admin" in help_lower
            or "management" in help_lower
            or "golden" in help_lower
        )
