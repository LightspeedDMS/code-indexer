"""Tests for CLI command group stubs - Story #735.

Tests that the new command group stubs exist and are properly configured.
"""

from click.testing import CliRunner
from code_indexer.cli import cli


class TestCommandGroupStubsExist:
    """Tests for command group stub existence."""

    def test_git_group_exists(self):
        """Test git command group exists in CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["git", "--help"])
        assert "No such command 'git'" not in result.output

    def test_files_group_exists(self):
        """Test files command group exists in CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["files", "--help"])
        assert "No such command 'files'" not in result.output

    def test_cicd_group_exists(self):
        """Test cicd command group exists in CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["cicd", "--help"])
        assert "No such command 'cicd'" not in result.output

    def test_groups_group_exists(self):
        """Test groups command group exists in CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["groups", "--help"])
        assert "No such command 'groups'" not in result.output

    def test_credentials_group_exists(self):
        """Test credentials command group exists in CLI."""
        runner = CliRunner()
        result = runner.invoke(cli, ["credentials", "--help"])
        assert "No such command 'credentials'" not in result.output

    def test_scip_group_already_exists(self):
        """Test scip command group already exists (from cli_scip.py)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["scip", "--help"])
        assert "No such command 'scip'" not in result.output
