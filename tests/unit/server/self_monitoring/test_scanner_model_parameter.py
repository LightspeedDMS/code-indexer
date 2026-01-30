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
            model="opus"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"status": "SUCCESS"}',
                stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Verify subprocess called with --model opus
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            assert "claude" in call_args
            assert "--model" in call_args
            model_index = call_args.index("--model")
            assert call_args[model_index + 1] == "opus"

    def test_invoke_claude_cli_includes_model_sonnet(self):
        """AC5: _invoke_claude_cli passes --model sonnet when model='sonnet'."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="sonnet"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"status": "SUCCESS"}',
                stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Verify subprocess called with --model sonnet
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            assert "claude" in call_args
            assert "--model" in call_args
            model_index = call_args.index("--model")
            assert call_args[model_index + 1] == "sonnet"

    def test_invoke_claude_cli_model_parameter_position(self):
        """AC5: Verify --model parameter appears before prompt input."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="opus"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"status": "SUCCESS"}',
                stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Verify command structure: claude --model <value> --json
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == "claude"
            # Model should be early in the command
            model_index = call_args.index("--model")
            assert model_index < len(call_args) - 1  # Not at the end

    def test_scanner_initialization_with_model(self):
        """AC5: LogScanner accepts model parameter in __init__."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="sonnet"
        )

        assert scanner.model == "sonnet"

    def test_scanner_model_defaults_to_opus_if_not_provided(self):
        """AC5: LogScanner defaults to 'opus' if model not specified."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test"
        )

        # Should default to opus
        assert scanner.model == "opus"

    def test_invoke_claude_cli_includes_allowed_tools_bash(self):
        """Claude CLI must have --allowedTools Bash to query log database via sqlite3."""
        scanner = LogScanner(
            db_path="/fake/db.db",
            scan_id="scan-123",
            github_repo="org/repo",
            log_db_path="/fake/logs.db",
            prompt_template="Test",
            model="opus"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"status": "SUCCESS"}',
                stderr=""
            )

            scanner._invoke_claude_cli("test prompt")

            # Verify subprocess called with --allowedTools Bash
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]  # Get the command list
            assert "--allowedTools" in call_args
            tools_index = call_args.index("--allowedTools")
            assert call_args[tools_index + 1] == "Bash"
