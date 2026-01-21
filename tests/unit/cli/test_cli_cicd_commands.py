"""Tests for CI/CD CLI commands - Story #746.

Tests the 12 cicd subcommands implemented for remote mode.
Following TDD methodology - write failing tests first.
"""

from click.testing import CliRunner


class TestCICDCommandGroupImport:
    """Test that cicd command group can be imported."""

    def test_cicd_group_can_be_imported(self):
        """Test cicd_group is importable from cli_cicd."""
        from code_indexer.cli_cicd import cicd_group

        assert cicd_group is not None

    def test_github_subgroup_can_be_imported(self):
        """Test github subgroup is importable."""
        from code_indexer.cli_cicd import github_group

        assert github_group is not None

    def test_gitlab_subgroup_can_be_imported(self):
        """Test gitlab subgroup is importable."""
        from code_indexer.cli_cicd import gitlab_group

        assert gitlab_group is not None


class TestCICDGitHubCommandsImport:
    """Test GitHub CI/CD commands can be imported."""

    def test_github_list_command_importable(self):
        """Test github_list command is importable."""
        from code_indexer.cli_cicd import github_list

        assert github_list is not None

    def test_github_show_command_importable(self):
        """Test github_show command is importable."""
        from code_indexer.cli_cicd import github_show

        assert github_show is not None

    def test_github_logs_command_importable(self):
        """Test github_logs command is importable."""
        from code_indexer.cli_cicd import github_logs

        assert github_logs is not None

    def test_github_job_logs_command_importable(self):
        """Test github_job_logs command is importable."""
        from code_indexer.cli_cicd import github_job_logs

        assert github_job_logs is not None

    def test_github_retry_command_importable(self):
        """Test github_retry command is importable."""
        from code_indexer.cli_cicd import github_retry

        assert github_retry is not None

    def test_github_cancel_command_importable(self):
        """Test github_cancel command is importable."""
        from code_indexer.cli_cicd import github_cancel

        assert github_cancel is not None


class TestCICDGitLabCommandsImport:
    """Test GitLab CI/CD commands can be imported."""

    def test_gitlab_list_command_importable(self):
        """Test gitlab_list command is importable."""
        from code_indexer.cli_cicd import gitlab_list

        assert gitlab_list is not None

    def test_gitlab_show_command_importable(self):
        """Test gitlab_show command is importable."""
        from code_indexer.cli_cicd import gitlab_show

        assert gitlab_show is not None

    def test_gitlab_logs_command_importable(self):
        """Test gitlab_logs command is importable."""
        from code_indexer.cli_cicd import gitlab_logs

        assert gitlab_logs is not None

    def test_gitlab_job_logs_command_importable(self):
        """Test gitlab_job_logs command is importable."""
        from code_indexer.cli_cicd import gitlab_job_logs

        assert gitlab_job_logs is not None

    def test_gitlab_retry_command_importable(self):
        """Test gitlab_retry command is importable."""
        from code_indexer.cli_cicd import gitlab_retry

        assert gitlab_retry is not None

    def test_gitlab_cancel_command_importable(self):
        """Test gitlab_cancel command is importable."""
        from code_indexer.cli_cicd import gitlab_cancel

        assert gitlab_cancel is not None


class TestCICDGitHubCommandHelp:
    """Test that GitHub CI/CD commands have correct help text and options."""

    def test_github_list_help(self):
        """Test github list command help."""
        from code_indexer.cli_cicd import github_list

        runner = CliRunner()
        result = runner.invoke(github_list, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        # Check for positional argument description
        assert "owner/repo" in help_text or "repository" in help_text

    def test_github_list_has_status_option(self):
        """Test github list has --status option."""
        from code_indexer.cli_cicd import github_list

        runner = CliRunner()
        result = runner.invoke(github_list, ["--help"])

        assert result.exit_code == 0
        assert "--status" in result.output

    def test_github_list_has_branch_option(self):
        """Test github list has --branch option."""
        from code_indexer.cli_cicd import github_list

        runner = CliRunner()
        result = runner.invoke(github_list, ["--help"])

        assert result.exit_code == 0
        assert "--branch" in result.output

    def test_github_list_has_limit_option(self):
        """Test github list has --limit option."""
        from code_indexer.cli_cicd import github_list

        runner = CliRunner()
        result = runner.invoke(github_list, ["--help"])

        assert result.exit_code == 0
        assert "--limit" in result.output

    def test_github_list_has_json_option(self):
        """Test github list has --json option."""
        from code_indexer.cli_cicd import github_list

        runner = CliRunner()
        result = runner.invoke(github_list, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output

    def test_github_show_help(self):
        """Test github show command help."""
        from code_indexer.cli_cicd import github_show

        runner = CliRunner()
        result = runner.invoke(github_show, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "run" in help_text or "id" in help_text

    def test_github_show_has_json_option(self):
        """Test github show has --json option."""
        from code_indexer.cli_cicd import github_show

        runner = CliRunner()
        result = runner.invoke(github_show, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output

    def test_github_logs_help(self):
        """Test github logs command help."""
        from code_indexer.cli_cicd import github_logs

        runner = CliRunner()
        result = runner.invoke(github_logs, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "run" in help_text or "log" in help_text

    def test_github_logs_has_query_option(self):
        """Test github logs has --query option."""
        from code_indexer.cli_cicd import github_logs

        runner = CliRunner()
        result = runner.invoke(github_logs, ["--help"])

        assert result.exit_code == 0
        assert "--query" in result.output

    def test_github_job_logs_help(self):
        """Test github job-logs command help."""
        from code_indexer.cli_cicd import github_job_logs

        runner = CliRunner()
        result = runner.invoke(github_job_logs, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "job" in help_text

    def test_github_retry_help(self):
        """Test github retry command help."""
        from code_indexer.cli_cicd import github_retry

        runner = CliRunner()
        result = runner.invoke(github_retry, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "run" in help_text or "retry" in help_text

    def test_github_cancel_help(self):
        """Test github cancel command help."""
        from code_indexer.cli_cicd import github_cancel

        runner = CliRunner()
        result = runner.invoke(github_cancel, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "run" in help_text or "cancel" in help_text


class TestCICDGitLabCommandHelp:
    """Test that GitLab CI/CD commands have correct help text and options."""

    def test_gitlab_list_help(self):
        """Test gitlab list command help."""
        from code_indexer.cli_cicd import gitlab_list

        runner = CliRunner()
        result = runner.invoke(gitlab_list, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "project" in help_text

    def test_gitlab_list_has_status_option(self):
        """Test gitlab list has --status option."""
        from code_indexer.cli_cicd import gitlab_list

        runner = CliRunner()
        result = runner.invoke(gitlab_list, ["--help"])

        assert result.exit_code == 0
        assert "--status" in result.output

    def test_gitlab_list_has_ref_option(self):
        """Test gitlab list has --ref option."""
        from code_indexer.cli_cicd import gitlab_list

        runner = CliRunner()
        result = runner.invoke(gitlab_list, ["--help"])

        assert result.exit_code == 0
        assert "--ref" in result.output

    def test_gitlab_list_has_limit_option(self):
        """Test gitlab list has --limit option."""
        from code_indexer.cli_cicd import gitlab_list

        runner = CliRunner()
        result = runner.invoke(gitlab_list, ["--help"])

        assert result.exit_code == 0
        assert "--limit" in result.output

    def test_gitlab_list_has_json_option(self):
        """Test gitlab list has --json option."""
        from code_indexer.cli_cicd import gitlab_list

        runner = CliRunner()
        result = runner.invoke(gitlab_list, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output

    def test_gitlab_show_help(self):
        """Test gitlab show command help."""
        from code_indexer.cli_cicd import gitlab_show

        runner = CliRunner()
        result = runner.invoke(gitlab_show, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "pipeline" in help_text or "id" in help_text

    def test_gitlab_show_has_json_option(self):
        """Test gitlab show has --json option."""
        from code_indexer.cli_cicd import gitlab_show

        runner = CliRunner()
        result = runner.invoke(gitlab_show, ["--help"])

        assert result.exit_code == 0
        assert "--json" in result.output

    def test_gitlab_logs_help(self):
        """Test gitlab logs command help."""
        from code_indexer.cli_cicd import gitlab_logs

        runner = CliRunner()
        result = runner.invoke(gitlab_logs, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "pipeline" in help_text or "log" in help_text

    def test_gitlab_logs_has_query_option(self):
        """Test gitlab logs has --query option."""
        from code_indexer.cli_cicd import gitlab_logs

        runner = CliRunner()
        result = runner.invoke(gitlab_logs, ["--help"])

        assert result.exit_code == 0
        assert "--query" in result.output

    def test_gitlab_job_logs_help(self):
        """Test gitlab job-logs command help."""
        from code_indexer.cli_cicd import gitlab_job_logs

        runner = CliRunner()
        result = runner.invoke(gitlab_job_logs, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "job" in help_text

    def test_gitlab_retry_help(self):
        """Test gitlab retry command help."""
        from code_indexer.cli_cicd import gitlab_retry

        runner = CliRunner()
        result = runner.invoke(gitlab_retry, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "pipeline" in help_text or "retry" in help_text

    def test_gitlab_cancel_help(self):
        """Test gitlab cancel command help."""
        from code_indexer.cli_cicd import gitlab_cancel

        runner = CliRunner()
        result = runner.invoke(gitlab_cancel, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "pipeline" in help_text or "cancel" in help_text


class TestCICDGroupHelp:
    """Test the cicd command group help."""

    def test_cicd_group_help(self):
        """Test cicd group has help text."""
        from code_indexer.cli_cicd import cicd_group

        runner = CliRunner()
        result = runner.invoke(cicd_group, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "github" in help_text
        assert "gitlab" in help_text

    def test_github_subgroup_help(self):
        """Test github subgroup has help text."""
        from code_indexer.cli_cicd import github_group

        runner = CliRunner()
        result = runner.invoke(github_group, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "list" in help_text
        assert "show" in help_text

    def test_gitlab_subgroup_help(self):
        """Test gitlab subgroup has help text."""
        from code_indexer.cli_cicd import gitlab_group

        runner = CliRunner()
        result = runner.invoke(gitlab_group, ["--help"])

        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "list" in help_text
        assert "show" in help_text
