"""Test individual command help and feature discovery (AC4-AC5) for Story #749.

Tests verify individual commands document all options and that
helpful guidance is provided when mode mismatches occur.
"""

from click.testing import CliRunner

from code_indexer.cli import cli


class TestIndividualCommandHelp:
    """AC4: Individual Command Help - all options documented."""

    def test_query_help_shows_core_options(self):
        """Verify query --help documents core options."""
        runner = CliRunner()
        result = runner.invoke(cli, ["query", "--help"])

        assert result.exit_code == 0
        output = result.output.lower()
        assert "--limit" in output or "-n" in output
        assert "--language" in output
        assert "--quiet" in output

    def test_query_help_shows_fts_options(self):
        """Verify query --help documents FTS options."""
        runner = CliRunner()
        result = runner.invoke(cli, ["query", "--help"])

        assert result.exit_code == 0
        output = result.output.lower()
        assert "--fts" in output
        assert "--semantic" in output

    def test_scip_definition_help_shows_options(self):
        """Verify scip definition --help shows options."""
        runner = CliRunner()
        result = runner.invoke(cli, ["scip", "definition", "--help"])

        assert result.exit_code == 0
        output = result.output.lower()
        assert "symbol" in output


class TestFeatureDiscoveryGuidance:
    """AC5: Feature Discovery Guidance - helpful messages for mode mismatches."""

    def test_remote_command_without_config_provides_guidance(self):
        """Verify remote commands provide guidance without config."""
        runner = CliRunner()

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["git", "status", "-r", "test"])

            assert result.exit_code != 0
            output = result.output.lower()
            # Should mention remote, init, connect, or configuration
            assert any(
                word in output
                for word in ["remote", "init", "connect", "configuration", "mode"]
            )

    def test_uninitialized_query_provides_guidance(self):
        """Verify uninitialized project query provides guidance."""
        runner = CliRunner()

        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["query", "test"])

            if result.exit_code != 0:
                output = result.output.lower()
                assert any(
                    word in output
                    for word in ["init", "configuration", "not found", "project"]
                )
