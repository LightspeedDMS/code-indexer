"""Tests for File CLI commands - Story #738.

Tests the 3 file subcommands implemented for remote mode.
Following TDD methodology - write failing tests first.
"""

from click.testing import CliRunner

from code_indexer.cli_files import (
    files_create,
    files_edit,
    files_delete,
)


class TestFilesCommandHelp:
    """Test that files commands have correct help text and options."""

    def test_files_create_help(self):
        """Test files create command help."""
        runner = CliRunner()
        result = runner.invoke(files_create, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
        # Should support either inline content or from file
        assert "content" in help_text or "from-file" in help_text

    def test_files_edit_help(self):
        """Test files edit command help."""
        runner = CliRunner()
        result = runner.invoke(files_edit, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
        assert "old" in help_text  # old string to replace
        assert "new" in help_text  # new string
        # Should support content-hash for optimistic locking
        assert "content-hash" in help_text or "hash" in help_text

    def test_files_delete_help(self):
        """Test files delete command help (requires --confirm)."""
        runner = CliRunner()
        result = runner.invoke(files_delete, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
        # Must require --confirm flag for destructive operation
        assert "confirm" in help_text


class TestFilesDeleteRequiresConfirm:
    """Test that files delete requires confirmation."""

    def test_files_delete_without_confirm_fails(self):
        """Test files delete fails without --confirm flag."""
        runner = CliRunner()
        # Try to delete without --confirm - should fail
        result = runner.invoke(
            files_delete,
            ["src/file.py", "-r", "test-repo"],
        )

        # Should fail because --confirm is required
        assert result.exit_code != 0
        # Should mention confirm in error message
        assert "confirm" in result.output.lower()


class TestFilesCreateOptions:
    """Test files create command options."""

    def test_files_create_requires_repository(self):
        """Test files create requires repository option."""
        runner = CliRunner()
        result = runner.invoke(
            files_create,
            ["src/file.py", "--content", "test content"],
        )

        # Should fail without repository
        assert result.exit_code != 0

    def test_files_create_requires_content_source(self):
        """Test files create requires either --content or --from-file."""
        runner = CliRunner()
        result = runner.invoke(
            files_create,
            ["src/file.py", "-r", "test-repo"],
        )

        # Should fail without content source
        assert result.exit_code != 0


class TestFilesEditOptions:
    """Test files edit command options."""

    def test_files_edit_requires_repository(self):
        """Test files edit requires repository option."""
        runner = CliRunner()
        result = runner.invoke(
            files_edit,
            ["src/file.py", "--old", "foo", "--new", "bar"],
        )

        # Should fail without repository
        assert result.exit_code != 0

    def test_files_edit_requires_old_string(self):
        """Test files edit requires --old option."""
        runner = CliRunner()
        result = runner.invoke(
            files_edit,
            ["src/file.py", "-r", "test-repo", "--new", "bar"],
        )

        # Should fail without --old
        assert result.exit_code != 0

    def test_files_edit_requires_new_string(self):
        """Test files edit requires --new option."""
        runner = CliRunner()
        result = runner.invoke(
            files_edit,
            ["src/file.py", "-r", "test-repo", "--old", "foo"],
        )

        # Should fail without --new
        assert result.exit_code != 0


class TestFilesCommandJsonOutput:
    """Test that files commands support --json flag."""

    def test_files_create_has_json_flag(self):
        """Test files create has --json flag."""
        runner = CliRunner()
        result = runner.invoke(files_create, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output or "json" in result.output.lower()

    def test_files_edit_has_json_flag(self):
        """Test files edit has --json flag."""
        runner = CliRunner()
        result = runner.invoke(files_edit, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output or "json" in result.output.lower()

    def test_files_delete_has_json_flag(self):
        """Test files delete has --json flag."""
        runner = CliRunner()
        result = runner.invoke(files_delete, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output or "json" in result.output.lower()
