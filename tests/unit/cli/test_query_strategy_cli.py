"""Unit tests for --strategy and --score-fusion CLI flag validation (Story #488 Phase 2).

Tests that:
- --strategy and --score-fusion flags are accepted by the CLI
- --score-fusion without --strategy parallel errors
- --strategy specific without --provider errors
- --strategy with --fts only (no semantic) errors
- Valid combinations are accepted without validation errors
"""

import pytest
from click.testing import CliRunner
from unittest.mock import patch
from pathlib import Path

from src.code_indexer.cli import cli


@pytest.fixture
def cli_runner():
    """Provide a CliRunner with find_project_root patched to a safe temp path."""
    runner = CliRunner()
    with patch("src.code_indexer.cli.find_project_root") as mock_find_root:
        mock_find_root.return_value = Path("/tmp/test-project")
        yield runner


def _invoke(runner, args):
    """Invoke the query command with the given extra args."""
    return runner.invoke(cli, ["query", "test query"] + args)


class TestStrategyFlagValidation:
    """Test validation errors for invalid --strategy / --score-fusion combinations."""

    def test_score_fusion_without_strategy_parallel_errors(self, cli_runner):
        """--score-fusion without --strategy parallel must error."""
        result = _invoke(cli_runner, ["--score-fusion", "rrf"])
        assert result.exit_code != 0
        output = result.output.lower()
        assert "score-fusion" in output or "strategy" in output, (
            f"Expected error mentioning score-fusion or strategy. Got: {result.output}"
        )

    def test_score_fusion_with_strategy_primary_only_errors(self, cli_runner):
        """--score-fusion with --strategy primary_only must error."""
        result = _invoke(
            cli_runner, ["--strategy", "primary_only", "--score-fusion", "rrf"]
        )
        assert result.exit_code != 0
        output = result.output.lower()
        assert "score-fusion" in output or "strategy" in output, (
            f"Expected error mentioning score-fusion or strategy. Got: {result.output}"
        )

    def test_score_fusion_with_strategy_failover_errors(self, cli_runner):
        """--score-fusion with --strategy failover must error."""
        result = _invoke(
            cli_runner, ["--strategy", "failover", "--score-fusion", "rrf"]
        )
        assert result.exit_code != 0
        output = result.output.lower()
        assert "score-fusion" in output or "strategy" in output, (
            f"Expected error mentioning score-fusion or strategy. Got: {result.output}"
        )

    def test_strategy_specific_without_provider_errors(self, cli_runner):
        """--strategy specific without --provider must error."""
        result = _invoke(cli_runner, ["--strategy", "specific"])
        assert result.exit_code != 0
        output = result.output.lower()
        assert "provider" in output or "specific" in output, (
            f"Expected error mentioning provider or specific. Got: {result.output}"
        )

    def test_strategy_with_fts_only_no_semantic_errors(self, cli_runner):
        """--strategy with --fts only (no --semantic) must error."""
        result = _invoke(cli_runner, ["--strategy", "failover", "--fts"])
        assert result.exit_code != 0
        output = result.output.lower()
        assert "fts" in output or "semantic" in output or "strategy" in output, (
            f"Expected error about fts/semantic/strategy. Got: {result.output}"
        )


class TestStrategyFlagAccepted:
    """Test that valid --strategy and --score-fusion combinations pass validation."""

    def test_strategy_primary_only_is_accepted(self, cli_runner):
        """--strategy primary_only must be a recognized flag."""
        result = _invoke(cli_runner, ["--strategy", "primary_only"])
        assert "no such option: --strategy" not in result.output, (
            f"--strategy flag not recognized. Got: {result.output}"
        )

    def test_strategy_failover_is_accepted(self, cli_runner):
        """--strategy failover must be a recognized flag."""
        result = _invoke(cli_runner, ["--strategy", "failover"])
        assert "no such option: --strategy" not in result.output, (
            f"--strategy flag not recognized. Got: {result.output}"
        )

    def test_strategy_parallel_with_score_fusion_rrf_is_accepted(self, cli_runner):
        """--strategy parallel --score-fusion rrf must pass validation."""
        result = _invoke(
            cli_runner, ["--strategy", "parallel", "--score-fusion", "rrf"]
        )
        assert "no such option: --strategy" not in result.output
        assert "no such option: --score-fusion" not in result.output
        output = result.output.lower()
        assert "score-fusion requires" not in output, (
            f"Unexpected score-fusion validation error. Got: {result.output}"
        )

    def test_strategy_parallel_with_score_fusion_multiply_is_accepted(self, cli_runner):
        """--strategy parallel --score-fusion multiply must pass validation."""
        result = _invoke(
            cli_runner, ["--strategy", "parallel", "--score-fusion", "multiply"]
        )
        assert "no such option: --strategy" not in result.output
        assert "no such option: --score-fusion" not in result.output

    def test_strategy_specific_with_provider_is_accepted(self, cli_runner):
        """--strategy specific with --provider must pass validation."""
        result = _invoke(
            cli_runner, ["--strategy", "specific", "--provider", "voyage-ai"]
        )
        assert "no such option: --strategy" not in result.output
        assert "--strategy specific requires --provider" not in result.output, (
            f"Unexpected validation error for valid combination. Got: {result.output}"
        )

    def test_score_fusion_average_with_parallel_is_accepted(self, cli_runner):
        """--strategy parallel --score-fusion average must pass validation."""
        result = _invoke(
            cli_runner, ["--strategy", "parallel", "--score-fusion", "average"]
        )
        assert "no such option: --score-fusion" not in result.output

    def test_strategy_with_fts_and_semantic_hybrid_is_accepted(self, cli_runner):
        """--strategy with both --fts and --semantic (hybrid mode) must pass validation."""
        result = _invoke(cli_runner, ["--strategy", "failover", "--fts", "--semantic"])
        assert "no such option: --strategy" not in result.output
