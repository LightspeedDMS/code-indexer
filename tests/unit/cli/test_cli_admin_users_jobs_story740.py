"""
Tests for CLI admin users and jobs commands - Story #740 requirements.

This test file validates:
1. All 6 admin users commands support --json output flag
2. All 3 admin jobs commands support --json output flag (including list if needed)
3. Delete command has --force flag for safety (users)
4. Cleanup command has --dry-run flag for safety (jobs)
5. Help text contains required information

Following TDD methodology - these tests define the expected behavior.
"""

import pytest
from unittest.mock import patch
from click.testing import CliRunner

from code_indexer.cli import cli


class TestAdminUsersJsonOutput:
    """Test --json output flag for all admin users commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_create_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin users create command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_list_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin users list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_show_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin users show command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "show", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_update_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin users update command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "update", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_delete_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin users delete command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_change_password_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin users change-password command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "change-password", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output


class TestAdminUsersSafetyFlags:
    """Test safety flags for admin users commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_delete_has_force_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin users delete command has --force flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--force" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_delete_force_flag_description(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that --force flag has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check that force flag is described
        help_lower = result.output.lower()
        assert "force" in help_lower
        assert (
            "skip" in help_lower
            or "confirmation" in help_lower
            or "prompt" in help_lower
        )


class TestAdminJobsJsonOutput:
    """Test --json output flag for all admin jobs commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_list_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin jobs list command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_stats_has_json_flag(self, mock_detect_mode, runner: CliRunner):
        """Test that admin jobs stats command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "stats", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_cleanup_has_json_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin jobs cleanup command has --json flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "cleanup", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--json" in result.output
        assert "Output as JSON" in result.output or "JSON" in result.output


class TestAdminJobsSafetyFlags:
    """Test safety flags for admin jobs commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_cleanup_has_dry_run_flag(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that admin jobs cleanup command has --dry-run flag."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "cleanup", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--dry-run" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_cleanup_dry_run_description(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test that --dry-run flag has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "cleanup", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check that dry-run flag is described
        help_lower = result.output.lower()
        assert "dry" in help_lower or "show" in help_lower
        assert "delete" in help_lower or "without" in help_lower


class TestAdminUsersHelpTexts:
    """Test help text requirements for admin users commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_create_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin users create help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "create", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        # Check required options mentioned
        assert "USERNAME" in result.output
        assert "--role" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_list_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin users list help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "list" in result.output.lower() or "users" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_show_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin users show help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "show", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "USERNAME" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_update_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin users update help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "update", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "USERNAME" in result.output
        assert "--role" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_delete_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin users delete help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "delete", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "USERNAME" in result.output
        assert "--force" in result.output

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_change_password_help(
        self, mock_detect_mode, runner: CliRunner
    ):
        """Test admin users change-password help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "change-password", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "USERNAME" in result.output
        assert "--password" in result.output


class TestAdminJobsHelpTexts:
    """Test help text requirements for admin jobs commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_list_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin jobs list help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "list", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "list" in result.output.lower() or "jobs" in result.output.lower()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_stats_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin jobs stats help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "stats", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert (
            "stats" in result.output.lower()
            or "statistics" in result.output.lower()
            or "analytics" in result.output.lower()
        )

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_cleanup_help(self, mock_detect_mode, runner: CliRunner):
        """Test admin jobs cleanup help contains required information."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "cleanup", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        assert "--older-than" in result.output
        assert "--dry-run" in result.output


class TestAdminCommandStructure:
    """Test that all admin commands exist and are properly structured."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create click test runner."""
        return CliRunner()

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_six_user_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 6 admin users commands exist."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        required_commands = [
            "create",
            "list",
            "show",
            "update",
            "delete",
            "change-password",
        ]
        for cmd in required_commands:
            assert (
                cmd in result.output
            ), f"Command '{cmd}' not found in admin users group"

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_all_three_jobs_commands_exist(self, mock_detect_mode, runner: CliRunner):
        """Test that all 3 admin jobs commands exist (list, stats, cleanup)."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"

        required_commands = ["list", "stats", "cleanup"]
        for cmd in required_commands:
            assert (
                cmd in result.output
            ), f"Command '{cmd}' not found in admin jobs group"

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_users_group_description(self, mock_detect_mode, runner: CliRunner):
        """Test admin users group has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "users", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        help_lower = result.output.lower()
        assert "user" in help_lower
        assert "management" in help_lower or "admin" in help_lower

    @patch("code_indexer.disabled_commands.detect_current_mode")
    def test_admin_jobs_group_description(self, mock_detect_mode, runner: CliRunner):
        """Test admin jobs group has proper description."""
        mock_detect_mode.return_value = "remote"
        result = runner.invoke(cli, ["admin", "jobs", "--help"])
        assert result.exit_code == 0, f"Failed: {result.output}"
        help_lower = result.output.lower()
        assert "job" in help_lower
        assert "management" in help_lower or "background" in help_lower
