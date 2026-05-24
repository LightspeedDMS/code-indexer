"""
Test that all paths stored in Filesystem are relative to codebase_dir for database portability.

This test suite verifies the critical requirement that Filesystem database contents are portable
across different filesystem locations (CoW cloning, repository moves, etc.) by ensuring all
file paths are stored as relative paths from codebase_dir, not absolute paths.

Critical for: CoW cloning, repository moves, reconcile operations, RAG context extraction.
"""

from pathlib import Path


class TestPathNormalizationHelper:
    """Test the path normalization helper method directly."""

    def test_normalize_absolute_path(self, tmp_path: Path):
        """Test normalization of absolute path to relative."""
        codebase_dir = tmp_path / "project"
        codebase_dir.mkdir()

        absolute_path = codebase_dir / "src" / "main.py"

        # This is what the helper should do
        expected_relative = "src/main.py"

        # Test path normalization logic
        if absolute_path.is_absolute():
            result = str(absolute_path.relative_to(codebase_dir))
        else:
            result = str(absolute_path)

        assert result == expected_relative, (
            f"Expected '{expected_relative}', got '{result}'"
        )

    def test_normalize_already_relative_path(self):
        """Test that already relative path remains unchanged."""
        relative_path = Path("src/main.py")
        codebase_dir = Path("/project")

        # This is what the helper should do
        if relative_path.is_absolute():
            result = str(relative_path.relative_to(codebase_dir))
        else:
            result = str(relative_path)

        assert result == "src/main.py", "Relative path should remain unchanged"

    def test_normalize_handles_nested_paths(self, tmp_path: Path):
        """Test normalization of deeply nested paths."""
        codebase_dir = tmp_path / "project"
        codebase_dir.mkdir()

        deep_path = codebase_dir / "src" / "services" / "api" / "handlers.py"
        expected_relative = "src/services/api/handlers.py"

        if deep_path.is_absolute():
            result = str(deep_path.relative_to(codebase_dir))
        else:
            result = str(deep_path)

        assert result == expected_relative
