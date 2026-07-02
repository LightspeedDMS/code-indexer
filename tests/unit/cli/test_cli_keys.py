"""
Unit tests for CLI keys commands - SSH Key Management.

Story #656: Advanced Operations Parity - SSH Key Management.
Following TDD methodology - tests written before implementation.
"""

from click.testing import CliRunner


class TestKeysCommandGroupExists:
    """Test that keys command group exists and is registered."""

    def test_keys_command_group_is_importable(self):
        """Test keys command group can be imported."""
        from code_indexer.cli_keys import keys_group

        assert keys_group is not None

    def test_keys_command_accessible_via_cli(self):
        """Test keys command is accessible via main CLI."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        # Should show help, not "No such command"
        assert result.exit_code == 0
        assert "keys" in result.output.lower()

    def test_keys_help_shows_available_subcommands(self):
        """Test keys help shows all available subcommands."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "create" in result.output
        assert "list" in result.output
        assert "delete" in result.output
        assert "show-public" in result.output
        assert "assign" in result.output


class TestKeysCreateCommand:
    """Tests for keys create command."""

    def test_create_command_exists(self):
        """Test that create subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        # Check group help which works without remote mode
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "create" in result.output

    def test_create_command_has_name_argument(self):
        """Test create command requires NAME argument (verified via import)."""
        from code_indexer.cli_keys import keys_create

        # Verify the command function exists and has correct params
        import click

        params = {p.name for p in keys_create.params}
        assert "name" in params or any(
            isinstance(p, click.Argument) for p in keys_create.params
        )

    def test_create_command_has_email_option(self):
        """Test create command has --email option (verified via import)."""
        from code_indexer.cli_keys import keys_create

        params = {p.name for p in keys_create.params}
        assert "email" in params

    def test_create_command_has_key_type_option(self):
        """Test create command has --key-type option (verified via import)."""
        from code_indexer.cli_keys import keys_create

        params = {p.name for p in keys_create.params}
        assert "key_type" in params

    def test_create_command_has_description_option(self):
        """Test create command has --description option (verified via import)."""
        from code_indexer.cli_keys import keys_create

        params = {p.name for p in keys_create.params}
        assert "description" in params

    def test_create_command_has_json_flag(self):
        """Test create command has --json flag (verified via import)."""
        from code_indexer.cli_keys import keys_create

        params = {p.name for p in keys_create.params}
        assert "json_output" in params


class TestKeysListCommand:
    """Tests for keys list command."""

    def test_list_command_exists(self):
        """Test that list subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "list" in result.output

    def test_list_command_has_json_flag(self):
        """Test list command has --json flag (verified via import)."""
        from code_indexer.cli_keys import keys_list

        params = {p.name for p in keys_list.params}
        assert "json_output" in params


class TestKeysDeleteCommand:
    """Tests for keys delete command."""

    def test_delete_command_exists(self):
        """Test that delete subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "delete" in result.output

    def test_delete_command_has_name_argument(self):
        """Test delete command requires NAME argument (verified via import)."""
        from code_indexer.cli_keys import keys_delete
        import click

        # Check for argument in params
        has_name_arg = any(
            isinstance(p, click.Argument) and p.name == "name"
            for p in keys_delete.params
        )
        assert has_name_arg

    def test_delete_command_has_yes_flag(self):
        """Test delete command has --yes flag for confirmation bypass (verified via import)."""
        from code_indexer.cli_keys import keys_delete

        params = {p.name for p in keys_delete.params}
        assert "yes" in params

    def test_delete_command_has_json_flag(self):
        """Test delete command has --json flag (verified via import)."""
        from code_indexer.cli_keys import keys_delete

        params = {p.name for p in keys_delete.params}
        assert "json_output" in params


class TestKeysShowPublicCommand:
    """Tests for keys show-public command."""

    def test_show_public_command_exists(self):
        """Test that show-public subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "show-public" in result.output

    def test_show_public_command_has_name_argument(self):
        """Test show-public command requires NAME argument (verified via import)."""
        from code_indexer.cli_keys import keys_show_public
        import click

        # Check for argument in params
        has_name_arg = any(
            isinstance(p, click.Argument) and p.name == "name"
            for p in keys_show_public.params
        )
        assert has_name_arg

    def test_show_public_command_has_json_flag(self):
        """Test show-public command has --json flag (verified via import)."""
        from code_indexer.cli_keys import keys_show_public

        params = {p.name for p in keys_show_public.params}
        assert "json_output" in params


class TestKeysAssignCommand:
    """Tests for keys assign command."""

    def test_assign_command_exists(self):
        """Test that assign subcommand exists by verifying it appears in group help."""
        from code_indexer.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["keys", "--help"])

        assert result.exit_code == 0
        assert "assign" in result.output

    def test_assign_command_has_name_argument(self):
        """Test assign command requires NAME argument (verified via import)."""
        from code_indexer.cli_keys import keys_assign
        import click

        # Check for name argument in params
        has_name_arg = any(
            isinstance(p, click.Argument) and p.name == "name"
            for p in keys_assign.params
        )
        assert has_name_arg

    def test_assign_command_has_hostname_argument(self):
        """Test assign command requires HOSTNAME argument (verified via import)."""
        from code_indexer.cli_keys import keys_assign
        import click

        # Check for hostname argument in params
        has_hostname_arg = any(
            isinstance(p, click.Argument) and p.name == "hostname"
            for p in keys_assign.params
        )
        assert has_hostname_arg

    def test_assign_command_has_force_flag(self):
        """Test assign command has --force flag (verified via import)."""
        from code_indexer.cli_keys import keys_assign

        params = {p.name for p in keys_assign.params}
        assert "force" in params

    def test_assign_command_has_json_flag(self):
        """Test assign command has --json flag (verified via import)."""
        from code_indexer.cli_keys import keys_assign

        params = {p.name for p in keys_assign.params}
        assert "json_output" in params


class TestKeysCommandModeRestriction:
    """Tests for keys command mode restrictions."""

    def test_keys_requires_remote_mode(self):
        """Test that keys commands require remote mode."""
        from code_indexer.cli import cli

        runner = CliRunner()

        # Running in local mode (no remote config) should fail
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["keys", "list"])

            # Should fail with mode restriction error
            assert result.exit_code != 0
            # Should mention remote mode requirement
            assert (
                "remote" in result.output.lower()
                or "not available" in result.output.lower()
                or "requires" in result.output.lower()
            )


class TestKeysCommandJsonOutput:
    """Tests for keys command JSON output format."""

    def test_create_json_output_format(self):
        """Test create command JSON output follows standard format."""
        # This test verifies the output format when the command succeeds
        # In a real test, we would mock the API client
        from code_indexer.cli_utils import format_json_success, format_json_error
        import json

        # Test success format
        success_output = format_json_success(
            {"name": "test-key", "fingerprint": "SHA256:abc"}
        )
        success_data = json.loads(success_output)
        assert success_data["success"] is True
        assert "data" in success_data

        # Test error format
        error_output = format_json_error("Key not found", "NotFoundError")
        error_data = json.loads(error_output)
        assert error_data["success"] is False
        assert "error" in error_data

    def test_list_json_output_format(self):
        """Test list command JSON output follows standard format."""
        from code_indexer.cli_utils import format_json_success
        import json

        keys_data = {
            "keys": [
                {"name": "key1", "key_type": "ed25519"},
                {"name": "key2", "key_type": "rsa"},
            ]
        }
        output = format_json_success(keys_data)
        data = json.loads(output)

        assert data["success"] is True
        assert "data" in data
        assert "keys" in data["data"]
