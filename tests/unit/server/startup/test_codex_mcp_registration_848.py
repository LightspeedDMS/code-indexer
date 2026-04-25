"""
Unit tests for _ensure_codex_mcp_registered in codex_cli_startup.py (Story #848).

Tests that CIDX MCP server registration is wired into Codex startup correctly.

Test inventory (5 tests across 3 classes):

  TestEnsureCodexMcpRegistered (2 tests)
    test_registration_invokes_correct_argv_shape
    test_registration_sets_codex_home_env

  TestEnsureCodexMcpRegisteredIdempotencyAndTimeout (2 tests)
    test_registration_is_idempotent
    test_timeout_logs_warning_and_does_not_raise

  TestInitializeCodexMcpRegistrationIntegration (1 test)
    test_registration_not_attempted_when_codex_disabled
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.startup.codex_cli_startup import (
    _ensure_codex_mcp_registered,
    initialize_codex_manager_on_startup,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_CIDX_MCP_COMMAND = "cidx mcp serve"
_CODEX_MCP_NAME = "cidx-local"
_EXPECTED_SUBPROCESS_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def codex_home(tmp_path: Path) -> Path:
    """Return a created codex-home directory under tmp_path."""
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Tests: core behaviour of _ensure_codex_mcp_registered
# ---------------------------------------------------------------------------


class TestEnsureCodexMcpRegistered:
    """_ensure_codex_mcp_registered invokes the right command with the right env."""

    def test_registration_invokes_correct_argv_shape(self, codex_home):
        """
        _ensure_codex_mcp_registered must call subprocess.run with:
          ["codex", "mcp", "add", "cidx-local", "--"] + <mcp_command_parts>
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        mock_run.assert_called_once()
        argv = mock_run.call_args.args[0]
        assert argv[:4] == ["codex", "mcp", "add", _CODEX_MCP_NAME], (
            f"First 4 args must be ['codex', 'mcp', 'add', 'cidx-local'], got {argv[:4]!r}"
        )
        assert argv[4] == "--", f"5th arg must be '--', got {argv[4]!r}"
        assert argv[5:] == _TEST_CIDX_MCP_COMMAND.split(), (
            f"Args after '--' must be command parts, got {argv[5:]!r}"
        )

    def test_registration_sets_codex_home_env(self, codex_home):
        """
        _ensure_codex_mcp_registered must set CODEX_HOME to the codex_home path
        in the subprocess environment.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        env_passed = mock_run.call_args.kwargs.get("env", {})
        assert env_passed.get("CODEX_HOME") == str(codex_home), (
            f"CODEX_HOME must be {str(codex_home)!r}, got {env_passed.get('CODEX_HOME')!r}"
        )


# ---------------------------------------------------------------------------
# Tests: idempotency and timeout behaviour
# ---------------------------------------------------------------------------


class TestEnsureCodexMcpRegisteredIdempotencyAndTimeout:
    """_ensure_codex_mcp_registered is idempotent and handles timeouts gracefully."""

    def test_registration_is_idempotent(self, codex_home):
        """
        Calling _ensure_codex_mcp_registered twice produces no error.
        subprocess.run is called twice (once per call) and both succeed.
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        assert mock_run.call_count == 2, (
            f"subprocess.run should be called once per _ensure_codex_mcp_registered call; "
            f"got {mock_run.call_count} calls"
        )

    def test_timeout_logs_warning_and_does_not_raise(self, codex_home, caplog):
        """
        When subprocess.run raises TimeoutExpired, _ensure_codex_mcp_registered
        logs a WARNING and does NOT re-raise the exception.
        """
        with patch("subprocess.run") as mock_run, caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.startup.codex_cli_startup",
        ):
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["codex", "mcp", "add"], timeout=_EXPECTED_SUBPROCESS_TIMEOUT
            )
            # Must not raise
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, (
            f"Expected a WARNING log on TimeoutExpired; got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Tests: integration with initialize_codex_manager_on_startup
# ---------------------------------------------------------------------------


class TestEnsureCodexMcpRegisteredEmptyCommand:
    """When cidx_mcp_command is empty, registration is skipped entirely (CRITICAL #1)."""

    def test_empty_command_skips_subprocess_no_call(self, codex_home):
        """
        When cidx_mcp_command is "" (the new placeholder default), _ensure_codex_mcp_registered
        must NOT call subprocess.run at all — the registration is silently skipped.
        """
        with patch("subprocess.run") as mock_run:
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command="",
            )
        mock_run.assert_not_called()

    def test_empty_command_logs_info_skip_message(self, codex_home, caplog):
        """
        When cidx_mcp_command is "", _ensure_codex_mcp_registered must emit exactly
        one INFO log record explaining that registration was skipped because no command
        is configured.
        """
        with patch("subprocess.run"), caplog.at_level(
            logging.INFO,
            logger="code_indexer.server.startup.codex_cli_startup",
        ):
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command="",
            )

        info_records = [
            r for r in caplog.records if r.levelno == logging.INFO and "skip" in r.message.lower()
        ]
        assert info_records, (
            f"Expected an INFO log mentioning skip when command is empty; "
            f"got: {[r.message for r in caplog.records]}"
        )


class TestEnsureCodexMcpRegisteredStderrCapture:
    """Non-zero returncode from subprocess includes stderr text in WARNING (HIGH #2)."""

    def test_nonzero_returncode_logs_stderr_text(self, codex_home, caplog):
        """
        When subprocess.run returns rc != 0 with stderr output, the WARNING log
        must contain the decoded stderr text so operators can diagnose failures.
        """
        error_text = "command not found: codex"
        with patch("subprocess.run") as mock_run, caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.startup.codex_cli_startup",
        ):
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr=error_text.encode("utf-8"),
            )
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected a WARNING log on non-zero returncode"
        combined = " ".join(r.message for r in warning_records)
        assert error_text in combined, (
            f"WARNING log must contain stderr text {error_text!r}; got: {combined!r}"
        )

    def test_zero_returncode_does_not_log_warning(self, codex_home, caplog):
        """
        When subprocess.run returns rc == 0, no WARNING log is emitted.
        """
        with patch("subprocess.run") as mock_run, caplog.at_level(
            logging.WARNING,
            logger="code_indexer.server.startup.codex_cli_startup",
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            _ensure_codex_mcp_registered(
                codex_home=codex_home,
                cidx_mcp_command=_TEST_CIDX_MCP_COMMAND,
            )

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warning_records, (
            f"No WARNING log expected on rc=0; got: {[r.message for r in warning_records]}"
        )


class TestInitializeCodexMcpRegistrationIntegration:
    """_ensure_codex_mcp_registered is not called when Codex is disabled."""

    def test_registration_not_attempted_when_codex_disabled(self, tmp_path):
        """
        When Codex integration is disabled (enabled=False), initialize_codex_manager_on_startup
        must NOT call _ensure_codex_mcp_registered (no subprocess.run for mcp add).
        """
        from code_indexer.server.utils.config_manager import CodexIntegrationConfig

        codex_cfg = CodexIntegrationConfig(enabled=False, credential_mode="none")
        server_config = MagicMock()
        server_config.codex_integration_config = codex_cfg

        with patch("subprocess.run") as mock_run:
            initialize_codex_manager_on_startup(
                server_config=server_config,
                server_data_dir=str(tmp_path),
            )

        mcp_add_calls = [
            c
            for c in mock_run.call_args_list
            if c.args and len(c.args[0]) >= 3 and c.args[0][:3] == ["codex", "mcp", "add"]
        ]
        assert not mcp_add_calls, (
            f"_ensure_codex_mcp_registered must not run when Codex disabled; "
            f"got subprocess calls: {mock_run.call_args_list}"
        )
