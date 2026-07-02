"""Test mode-aware CLI help output (AC1-AC3) for Story #749.

Tests verify mode indicators display correctly in help output,
remote-only commands are highlighted, and command group help works.
"""

from click.testing import CliRunner

from code_indexer.cli import cli


class TestModeAwareHelpOutput:
    """AC1: Mode-Aware Help Output - commands show mode indicators."""

    def test_help_shows_mode_legend(self):
        """Verify --help shows mode legend at bottom."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Legend:" in result.output

    def test_help_shows_remote_indicator(self):
        """Verify remote mode indicator appears in help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Remote" in result.output or "REMOTE" in result.output

    def test_help_shows_local_indicator(self):
        """Verify local mode indicator appears in help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "Local" in result.output or "LOCAL" in result.output


class TestRemoteModeHelp:
    """AC2: Remote Mode Help - remote commands highlighted appropriately."""

    def test_git_group_help_mentions_remote(self):
        """Verify git group docstring mentions remote mode."""
        from code_indexer.cli_git import git_group

        assert "remote" in git_group.help.lower()

    def test_files_group_help_mentions_remote(self):
        """Verify files group docstring mentions remote mode."""
        from code_indexer.cli_files import files_group

        assert "remote" in files_group.help.lower()

    def test_cicd_group_help_mentions_remote(self):
        """Verify cicd group docstring mentions remote mode."""
        from code_indexer.cli_cicd import cicd_group

        assert "remote" in cicd_group.help.lower()


class TestCommandGroupHelp:
    """AC3: Command Group Help - subcommands listed with descriptions."""

    def test_scip_group_help_lists_subcommands(self):
        """Verify scip --help lists all subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["scip", "--help"])

        assert result.exit_code == 0
        output = result.output.lower()
        assert "definition" in output or "def" in output
        assert "references" in output or "refs" in output

    def test_main_help_lists_command_groups(self):
        """Verify main --help lists command groups."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        output = result.output.lower()
        assert "scip" in output
        assert "git" in output
        assert "files" in output
        assert "cicd" in output
