"""Test command groups and JSON output documentation (AC7-AC8) for Story #749.

Tests verify all command groups have complete documentation
and JSON output flags are properly documented.
"""

from click.testing import CliRunner

from code_indexer.cli import cli


class TestCommandGroupsDocumented:
    """AC7: New Command Groups Documented - complete documentation."""

    def test_git_group_has_complete_docstring(self):
        """Verify git group has complete documentation."""
        from code_indexer.cli_git import git_group

        assert git_group.help is not None
        assert len(git_group.help) > 20  # Not just a stub
        assert "git" in git_group.help.lower()

    def test_files_group_has_complete_docstring(self):
        """Verify files group has complete documentation."""
        from code_indexer.cli_files import files_group

        assert files_group.help is not None
        assert len(files_group.help) > 20
        assert "file" in files_group.help.lower()

    def test_cicd_group_has_complete_docstring(self):
        """Verify cicd group has complete documentation."""
        from code_indexer.cli_cicd import cicd_group

        assert cicd_group.help is not None
        assert len(cicd_group.help) > 20
        assert "ci" in cicd_group.help.lower() or "pipeline" in cicd_group.help.lower()


class TestJSONOutputDocumented:
    """AC8: JSON Output Documentation - --json flag documented."""

    def test_server_list_indexes_documents_json_flag(self):
        """Verify server list-indexes --help documents --json flag."""
        runner = CliRunner()
        result = runner.invoke(cli, ["server", "list-indexes", "--help"])

        assert result.exit_code == 0
        assert "--json" in result.output

    def test_git_commands_document_json_flag(self):
        """Verify git command modules document --json flag."""
        from code_indexer.cli_git import git_status

        # Check that the git_status function has json_output parameter
        import inspect

        sig = inspect.signature(git_status.callback)
        param_names = list(sig.parameters.keys())
        assert "json_output" in param_names
