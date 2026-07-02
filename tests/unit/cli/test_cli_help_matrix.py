"""Test command matrix documentation (AC6) for Story #749.

Tests verify 'cidx help commands' and 'cidx help matrix' commands exist
and display complete command availability information.

These tests are expected to FAIL until the help command group is implemented.
"""

from click.testing import CliRunner

from code_indexer.cli import cli


class TestHelpCommandGroup:
    """AC6: Command Matrix Documentation - help subcommands exist."""

    def test_help_command_group_exists(self):
        """Verify 'cidx help' command group exists."""
        runner = CliRunner()
        result = runner.invoke(cli, ["help", "--help"])

        # Should succeed with help subcommand list
        assert result.exit_code == 0, f"Expected help group to exist: {result.output}"
        assert "commands" in result.output.lower() or "matrix" in result.output.lower()

    def test_help_commands_lists_all_commands(self):
        """Verify 'cidx help commands' lists all commands with modes."""
        runner = CliRunner()
        result = runner.invoke(cli, ["help", "commands"])

        assert result.exit_code == 0, f"Expected help commands to work: {result.output}"
        output = result.output.lower()

        # Should list common commands
        assert "query" in output
        assert "index" in output
        # Should show mode information
        assert "local" in output or "remote" in output

    def test_help_matrix_shows_table(self):
        """Verify 'cidx help matrix' shows availability matrix."""
        runner = CliRunner()
        result = runner.invoke(cli, ["help", "matrix"])

        assert result.exit_code == 0, f"Expected help matrix to work: {result.output}"
        output = result.output.lower()

        # Should show table headers
        assert "local" in output
        assert "remote" in output
        # Should list commands
        assert "query" in output
