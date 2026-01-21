"""Tests for Git CLI commands - Story #737.

Tests the 23 git subcommands implemented for remote mode.
Following TDD methodology - write failing tests first.
"""

from click.testing import CliRunner

from code_indexer.cli_git import (
    git_status,
    git_commit,
    git_reset,
    git_diff,
    git_log,
    git_show,
    git_stage,
    git_unstage,
    git_push,
    git_pull,
    git_fetch,
    git_clean,
    git_merge_abort,
    git_checkout_file,
    git_branches,
    git_branch_create,
    git_branch_switch,
    git_branch_delete,
    git_blame,
    git_file_history,
    git_search_commits,
    git_search_diffs,
    git_cat,
)


class TestGitCommandHelp:
    """Test that git commands have correct help text and options."""

    def test_git_status_help(self):
        """Test git status command help (read-only operation)."""
        runner = CliRunner()
        result = runner.invoke(git_status, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_commit_help(self):
        """Test git commit command help (write operation)."""
        runner = CliRunner()
        result = runner.invoke(git_commit, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_reset_help(self):
        """Test git reset command help (destructive operation)."""
        runner = CliRunner()
        result = runner.invoke(git_reset, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_diff_help(self):
        """Test git diff command help."""
        runner = CliRunner()
        result = runner.invoke(git_diff, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_log_help(self):
        """Test git log command help."""
        runner = CliRunner()
        result = runner.invoke(git_log, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_show_help(self):
        """Test git show command help."""
        runner = CliRunner()
        result = runner.invoke(git_show, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_stage_help(self):
        """Test git stage command help."""
        runner = CliRunner()
        result = runner.invoke(git_stage, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_unstage_help(self):
        """Test git unstage command help."""
        runner = CliRunner()
        result = runner.invoke(git_unstage, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_push_help(self):
        """Test git push command help."""
        runner = CliRunner()
        result = runner.invoke(git_push, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_pull_help(self):
        """Test git pull command help."""
        runner = CliRunner()
        result = runner.invoke(git_pull, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_fetch_help(self):
        """Test git fetch command help."""
        runner = CliRunner()
        result = runner.invoke(git_fetch, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_clean_help(self):
        """Test git clean command help (destructive operation)."""
        runner = CliRunner()
        result = runner.invoke(git_clean, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
        assert "confirm" in help_text

    def test_git_merge_abort_help(self):
        """Test git merge-abort command help."""
        runner = CliRunner()
        result = runner.invoke(git_merge_abort, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_checkout_file_help(self):
        """Test git checkout-file command help."""
        runner = CliRunner()
        result = runner.invoke(git_checkout_file, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_branches_help(self):
        """Test git branches command help."""
        runner = CliRunner()
        result = runner.invoke(git_branches, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_branch_create_help(self):
        """Test git branch-create command help."""
        runner = CliRunner()
        result = runner.invoke(git_branch_create, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_branch_switch_help(self):
        """Test git branch-switch command help."""
        runner = CliRunner()
        result = runner.invoke(git_branch_switch, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_branch_delete_help(self):
        """Test git branch-delete command help (destructive operation)."""
        runner = CliRunner()
        result = runner.invoke(git_branch_delete, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
        assert "confirm" in help_text

    def test_git_blame_help(self):
        """Test git blame command help."""
        runner = CliRunner()
        result = runner.invoke(git_blame, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_file_history_help(self):
        """Test git file-history command help."""
        runner = CliRunner()
        result = runner.invoke(git_file_history, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_search_commits_help(self):
        """Test git search-commits command help."""
        runner = CliRunner()
        result = runner.invoke(git_search_commits, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_search_diffs_help(self):
        """Test git search-diffs command help."""
        runner = CliRunner()
        result = runner.invoke(git_search_diffs, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text

    def test_git_cat_help(self):
        """Test git cat command help."""
        runner = CliRunner()
        result = runner.invoke(git_cat, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "repository" in help_text or "-r" in help_text
