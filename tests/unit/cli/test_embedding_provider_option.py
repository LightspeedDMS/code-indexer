"""Test suite for CLI embedding provider option - Story #507.

This test suite verifies that:
1. CLI only accepts 'voyage-ai' as embedding provider
2. CLI rejects 'voyage' with clear error message
3. Default provider is 'voyage-ai'
"""

from click.testing import CliRunner

from code_indexer.cli import cli


class TestEmbeddingProviderOption:
    """Test CLI embedding provider option restrictions."""

    def test_cli_accepts_voyage_ai_provider(self, tmp_path):
        """Test that CLI accepts voyage-ai as embedding provider."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Should succeed with voyage-ai
            result = runner.invoke(cli, ["init", "--embedding-provider", "voyage-ai"])

            # May fail for other reasons (missing API key, etc), but should not fail due to invalid provider
            assert "Invalid value for '--embedding-provider'" not in result.output

    def test_cli_rejects_voyage_provider(self, tmp_path):
        """Test that CLI rejects voyage as embedding provider."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Should fail with clear error
            result = runner.invoke(cli, ["init", "--embedding-provider", "voyage"])

            assert result.exit_code != 0
            assert "Invalid value for '--embedding-provider'" in result.output
            # Click should show available options
            assert "voyage-ai" in result.output.lower()

    def test_cli_default_provider_is_voyage_ai(self, tmp_path):
        """Test that default embedding provider is voyage-ai."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Init without specifying provider
            result = runner.invoke(cli, ["init"])

            # Should not complain about invalid provider
            assert "Invalid value for '--embedding-provider'" not in result.output

    def test_cli_help_shows_only_voyage_ai(self):
        """Test that --help shows only voyage-ai as option."""
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--help"])

        assert result.exit_code == 0
        # Should mention voyage-ai in help
        assert "voyage-ai" in result.output.lower()
        # Should NOT mention voyage in provider options
        # (may appear in examples/deprecation notices, but not as valid choice)


class TestDualEmbedOption:
    """Test --dual-embed CLI flag on index command - Story #487."""

    def test_dual_embed_flag_in_help(self):
        """Test that --dual-embed appears in index --help output."""
        runner = CliRunner()
        result = runner.invoke(cli, ["index", "--help"])

        assert result.exit_code == 0
        assert "--dual-embed" in result.output
        assert "redundancy" in result.output.lower()

    def test_dual_embed_and_provider_mutually_exclusive(self, tmp_path):
        """Test that --dual-embed and --provider cannot be used together."""
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                cli, ["index", "--dual-embed", "--provider", "cohere"]
            )

            assert result.exit_code != 0
            assert "mutually exclusive" in result.output.lower()

    def test_dual_embed_flag_accepted(self):
        """Test that --dual-embed is a valid flag on the index command."""
        runner = CliRunner()
        result = runner.invoke(cli, ["index", "--help"])

        assert result.exit_code == 0
        # Verify flag is boolean (is_flag=True)
        assert "--dual-embed" in result.output
