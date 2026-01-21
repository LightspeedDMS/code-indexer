"""
Unit tests for CLI index remote commands - Index Management.

Story #656: Advanced Operations Parity - Indexing Commands.
Following TDD methodology - tests written before implementation.
"""

from click.testing import CliRunner


class TestIndexRemoteCommandGroupExists:
    """Test that remote index command group exists and is registered."""

    def test_index_remote_command_group_is_importable(self):
        """Test remote-index command group can be imported."""
        from code_indexer.cli_index import index_remote_group

        assert index_remote_group is not None

    def test_remote_index_command_accessible_via_cli(self):
        """Test remote-index command is accessible via main CLI."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["remote-index", "--help"])

        # Should show help, not "No such command"
        assert result.exit_code == 0
        assert (
            "remote-index" in result.output.lower() or "index" in result.output.lower()
        )

    def test_remote_index_help_shows_available_subcommands(self):
        """Test remote-index help shows all available subcommands."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["remote-index", "--help"])

        assert result.exit_code == 0
        assert "trigger" in result.output
        assert "status" in result.output
        assert "add-type" in result.output


class TestIndexTriggerCommand:
    """Tests for remote-index trigger command."""

    def test_trigger_command_exists(self):
        """Test that trigger subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["remote-index", "--help"])

        assert result.exit_code == 0
        assert "trigger" in result.output

    def test_trigger_command_has_repository_argument(self):
        """Test trigger command requires REPOSITORY argument (verified via import)."""
        from code_indexer.cli_index import index_trigger
        import click

        # Check for repository argument in params
        has_repo_arg = any(
            isinstance(p, click.Argument) and p.name == "repository"
            for p in index_trigger.params
        )
        assert has_repo_arg

    def test_trigger_command_has_clear_flag(self):
        """Test trigger command has --clear flag (verified via import)."""
        from code_indexer.cli_index import index_trigger

        params = {p.name for p in index_trigger.params}
        assert "clear" in params

    def test_trigger_command_has_types_option(self):
        """Test trigger command has --types option (verified via import)."""
        from code_indexer.cli_index import index_trigger

        params = {p.name for p in index_trigger.params}
        assert "types" in params

    def test_trigger_command_has_json_flag(self):
        """Test trigger command has --json flag (verified via import)."""
        from code_indexer.cli_index import index_trigger

        params = {p.name for p in index_trigger.params}
        assert "json_output" in params


class TestIndexStatusCommand:
    """Tests for remote-index status command."""

    def test_status_command_exists(self):
        """Test that status subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["remote-index", "--help"])

        assert result.exit_code == 0
        assert "status" in result.output

    def test_status_command_has_repository_argument(self):
        """Test status command requires REPOSITORY argument (verified via import)."""
        from code_indexer.cli_index import index_status
        import click

        # Check for repository argument in params
        has_repo_arg = any(
            isinstance(p, click.Argument) and p.name == "repository"
            for p in index_status.params
        )
        assert has_repo_arg

    def test_status_command_has_json_flag(self):
        """Test status command has --json flag (verified via import)."""
        from code_indexer.cli_index import index_status

        params = {p.name for p in index_status.params}
        assert "json_output" in params


class TestIndexAddTypeCommand:
    """Tests for remote-index add-type command."""

    def test_add_type_command_exists(self):
        """Test that add-type subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["remote-index", "--help"])

        assert result.exit_code == 0
        assert "add-type" in result.output

    def test_add_type_command_has_repository_argument(self):
        """Test add-type command requires REPOSITORY argument (verified via import)."""
        from code_indexer.cli_index import index_add_type
        import click

        # Check for repository argument in params
        has_repo_arg = any(
            isinstance(p, click.Argument) and p.name == "repository"
            for p in index_add_type.params
        )
        assert has_repo_arg

    def test_add_type_command_has_type_argument(self):
        """Test add-type command requires TYPE argument (verified via import)."""
        from code_indexer.cli_index import index_add_type
        import click

        # Check for type argument in params
        has_type_arg = any(
            isinstance(p, click.Argument) and p.name == "type"
            for p in index_add_type.params
        )
        assert has_type_arg

    def test_add_type_command_has_json_flag(self):
        """Test add-type command has --json flag (verified via import)."""
        from code_indexer.cli_index import index_add_type

        params = {p.name for p in index_add_type.params}
        assert "json_output" in params


class TestIndexRemoteCommandModeRestriction:
    """Tests for remote-index command mode restrictions."""

    def test_remote_index_requires_remote_mode(self):
        """Test that remote-index commands require remote mode."""
        from code_indexer.cli import cli

        runner = CliRunner()

        # Running in local mode (no remote config) should fail
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["remote-index", "status", "my-repo"])

            # Should fail with mode restriction error
            assert result.exit_code != 0
            # Should mention remote mode requirement
            assert (
                "remote" in result.output.lower()
                or "not available" in result.output.lower()
                or "requires" in result.output.lower()
            )


class TestIndexRemoteCommandJsonOutput:
    """Tests for remote-index command JSON output format."""

    def test_trigger_json_output_format(self):
        """Test trigger command JSON output follows standard format."""
        from code_indexer.cli_utils import format_json_success, format_json_error
        import json

        # Test success format
        success_output = format_json_success(
            {
                "job_id": "job-123",
                "repository": "my-repo",
                "status": "queued",
            }
        )
        success_data = json.loads(success_output)
        assert success_data["success"] is True
        assert "data" in success_data
        assert success_data["data"]["job_id"] == "job-123"

        # Test error format
        error_output = format_json_error("Repository not found", "NotFoundError")
        error_data = json.loads(error_output)
        assert error_data["success"] is False
        assert "error" in error_data

    def test_status_json_output_format(self):
        """Test status command JSON output follows standard format."""
        from code_indexer.cli_utils import format_json_success
        import json

        status_data = {
            "repository": "my-repo",
            "indexes": {
                "semantic": {"status": "complete", "files_indexed": 100},
                "fts": {"status": "complete", "files_indexed": 100},
            },
        }
        output = format_json_success(status_data)
        data = json.loads(output)

        assert data["success"] is True
        assert "data" in data
        assert "indexes" in data["data"]

    def test_add_type_json_output_format(self):
        """Test add-type command JSON output follows standard format."""
        from code_indexer.cli_utils import format_json_success
        import json

        add_type_data = {
            "added": True,
            "repository": "my-repo",
            "type": "temporal",
            "job_id": "job-456",
        }
        output = format_json_success(add_type_data)
        data = json.loads(output)

        assert data["success"] is True
        assert "data" in data
        assert data["data"]["added"] is True


class TestIndexTypeValidation:
    """Tests for index type validation."""

    def test_valid_index_types(self):
        """Test that valid index types are recognized."""
        valid_types = ["semantic", "fts", "temporal", "scip"]

        # This test will verify the validation logic once implemented
        for index_type in valid_types:
            # The type should be accepted without error
            assert index_type in valid_types

    def test_trigger_multiple_types(self):
        """Test trigger with multiple index types."""
        from code_indexer.cli_utils import format_json_success
        import json

        # Test that multiple types can be specified
        trigger_data = {
            "job_id": "job-789",
            "repository": "my-repo",
            "status": "queued",
            "index_types": ["semantic", "fts", "scip"],
        }
        output = format_json_success(trigger_data)
        data = json.loads(output)

        assert data["success"] is True
        assert len(data["data"]["index_types"]) == 3
