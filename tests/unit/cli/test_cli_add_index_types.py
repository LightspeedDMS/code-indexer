"""
Unit tests for CLI add-index command index types.

Verifies that the CLI uses individual index types:
- semantic (separate from FTS)
- fts (separate from semantic)
- temporal
- scip

NOT the old combined: semantic_fts

Story #2: Fix Add Index functionality - HIGH-1
"""

import pytest
from click.testing import CliRunner
from code_indexer.cli import cli


class TestAddIndexCliIndexTypes:
    """Tests for CLI add-index command index types."""

    @pytest.fixture
    def runner(self):
        """Create a Click test runner."""
        return CliRunner()

    def test_cli_add_index_accepts_semantic(self, runner):
        """Test that CLI accepts 'semantic' as valid index type."""
        # Use --help to check if semantic is listed as a choice
        # This will show the valid choices in the error message
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        # Check that semantic is in the help output as a choice
        assert (
            "semantic" in result.output.lower()
        ), "CLI add-index should accept 'semantic' as a valid index type"

    def test_cli_add_index_accepts_fts(self, runner):
        """Test that CLI accepts 'fts' as valid index type."""
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        # Check that fts is in the help output as a choice
        assert (
            "fts" in result.output.lower()
        ), "CLI add-index should accept 'fts' as a valid index type"

    def test_cli_add_index_accepts_temporal(self, runner):
        """Test that CLI accepts 'temporal' as valid index type."""
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        assert (
            "temporal" in result.output.lower()
        ), "CLI add-index should accept 'temporal' as a valid index type"

    def test_cli_add_index_accepts_scip(self, runner):
        """Test that CLI accepts 'scip' as valid index type."""
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        assert (
            "scip" in result.output.lower()
        ), "CLI add-index should accept 'scip' as a valid index type"

    def test_cli_add_index_rejects_semantic_fts(self, runner):
        """
        HIGH-1: Test that CLI rejects 'semantic_fts' combined type.

        The CLI should use separate 'semantic' and 'fts' types,
        not the old combined 'semantic_fts'.
        """
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        # The help output should NOT list semantic_fts as a valid choice
        # Click shows choices in format like: [semantic|fts|temporal|scip]
        assert "semantic_fts" not in result.output, (
            "CRITICAL: CLI still uses 'semantic_fts' combined type. "
            "Should use separate 'semantic' and 'fts' types. "
            f"Output: {result.output}"
        )

    def test_cli_add_index_help_shows_all_individual_types(self, runner):
        """Test that help shows all four individual index types."""
        result = runner.invoke(cli, ["server", "add-index", "--help"])

        output_lower = result.output.lower()

        # All four individual types should be mentioned
        assert "semantic" in output_lower, "Missing 'semantic' in help"
        assert "fts" in output_lower, "Missing 'fts' in help"
        assert "temporal" in output_lower, "Missing 'temporal' in help"
        assert "scip" in output_lower, "Missing 'scip' in help"

    def test_cli_get_indexes_shows_individual_types(self, runner):
        """
        Test that get-indexes command displays individual types.

        Should show semantic and fts separately, not as semantic_fts.
        """
        result = runner.invoke(cli, ["server", "get-indexes", "--help"])

        # Help should not prominently reference semantic_fts
        # Allow for deprecation notices, but not as the primary display
        if "semantic_fts" in result.output:
            # If it appears, it should be in a deprecation context
            assert (
                "deprecated" in result.output.lower()
                or "separate" in result.output.lower()
                or result.output.count("semantic_fts") < 2
            ), (
                "CLI get-indexes still prominently uses 'semantic_fts'. "
                "Should display 'semantic' and 'fts' separately."
            )


class TestIndexStatusDisplayTypes:
    """Tests for how index status is displayed in CLI."""

    @pytest.fixture
    def runner(self):
        """Create a Click test runner."""
        return CliRunner()

    def test_index_types_constant_not_semantic_fts(self):
        """
        Verify that CLI internal constants don't use semantic_fts.

        This tests that code displaying index status iterates over
        individual types, not the combined semantic_fts.
        """
        # Import the CLI module to check its internals
        import code_indexer.cli as cli_module
        import inspect

        # Get the source code of the server_get_indexes function
        # or any function that displays index status
        source = inspect.getsource(cli_module)

        # Count occurrences in iteration/display contexts
        # We're looking for patterns like: for index_type in ["semantic_fts"...
        # which should be: for index_type in ["semantic", "fts"...

        # Find lines that iterate over index types for display
        display_lines = [
            line
            for line in source.split("\n")
            if "for index_type in" in line and "[" in line
        ]

        for line in display_lines:
            assert "semantic_fts" not in line, (
                f"CLI still iterates over 'semantic_fts' for display: {line.strip()}\n"
                "Should iterate over individual types: semantic, fts, temporal, scip"
            )
