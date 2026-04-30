"""
Tests for LogScanner model parameter (Story #76 - AC5).

Verifies that LogScanner passes --model parameter to Claude CLI
based on SelfMonitoringConfig.model setting.
"""

from unittest.mock import MagicMock, patch


from code_indexer.server.self_monitoring.scanner import LogScanner


class TestLogScannerModelParameter:
    """Test suite for LogScanner --model parameter in Claude CLI invocation."""

    def test_invoke_claude_cli_includes_model_opus(self):
        """AC5: _invoke_claude_cli passes --model opus when model='opus'."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="opus",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"status": "SUCCESS"}', stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Command is wrapped: ['script', '-q', '-c', "timeout N claude --model M ...", '/dev/null']
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            shell_cmd = call_args[3]  # inner shell command string
            assert "claude" in shell_cmd
            assert "--model opus" in shell_cmd

    def test_invoke_claude_cli_includes_model_sonnet(self):
        """AC5: _invoke_claude_cli passes --model sonnet when model='sonnet'."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="sonnet",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"status": "SUCCESS"}', stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Command is wrapped: ['script', '-q', '-c', "timeout N claude --model M ...", '/dev/null']
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            shell_cmd = call_args[3]  # inner shell command string
            assert "claude" in shell_cmd
            assert "--model sonnet" in shell_cmd

    def test_invoke_claude_cli_model_parameter_position(self):
        """AC5: Verify --model parameter appears before prompt input."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="opus",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"status": "SUCCESS"}', stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Command is wrapped: ['script', '-q', '-c', "timeout N claude --model M ...", '/dev/null']
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == "script"  # outer wrapper
            shell_cmd = call_args[3]  # inner shell command string
            # Model should appear before the prompt (-p flag)
            model_pos = shell_cmd.index("--model")
            prompt_pos = shell_cmd.index(" -p ")
            assert model_pos < prompt_pos

    def test_scanner_initialization_with_model(self):
        """AC5: LogScanner accepts model parameter in __init__."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="sonnet",
        )

        assert scanner.model == "sonnet"

    def test_scanner_model_defaults_to_opus_if_not_provided(self):
        """AC5: LogScanner defaults to 'opus' if model not specified."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
        )

        # Should default to opus
        assert scanner.model == "opus"

    def test_invoke_claude_cli_includes_dangerously_skip_permissions(self):
        """Claude CLI must skip permission prompts so Bash (sqlite3) runs unattended.

        The old --allowedTools Bash flag was removed when the scanner was refactored
        to route through the CliDispatcher. The equivalent grant is now provided via
        --dangerously-skip-permissions which allows all tools including Bash.
        """
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="opus",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout='{"status": "SUCCESS"}', stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Command is wrapped: ['script', '-q', '-c', "timeout N claude ... --dangerously-skip-permissions", '/dev/null']
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            shell_cmd = call_args[3]  # inner shell command string
            assert "--dangerously-skip-permissions" in shell_cmd
