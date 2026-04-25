"""
Unit tests for _login_codex_with_api_key (api_key mode auth delegation).

Verifies that api_key mode delegates auth.json population to
`codex login --with-api-key` (reading the key from stdin) rather than
writing the OAuth-style schema via CodexCredentialsFileManager.

All subprocess calls are mocked — no actual `codex` binary is invoked.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from code_indexer.server.startup.codex_cli_startup import _login_codex_with_api_key


TEST_API_KEY = "test-api-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed_process(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock CompletedProcess-like object."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoginCodexWithApiKey:
    def test_login_calls_codex_with_correct_args(self, tmp_path):
        """subprocess.run must be called with ['codex', 'login', '--with-api-key']."""
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["codex", "login", "--with-api-key"]

    def test_login_passes_api_key_via_stdin(self, tmp_path):
        """api_key must be passed as stdin bytes, NOT on the command line."""
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["input"] == TEST_API_KEY.encode("utf-8")
        cmd = mock_run.call_args[0][0]
        assert TEST_API_KEY not in cmd

    def test_login_sets_codex_home_in_env(self, tmp_path):
        """CODEX_HOME env var must be set to str(codex_home) in the subprocess env."""
        with patch("subprocess.run", return_value=_make_completed_process()) as mock_run:
            _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        call_kwargs = mock_run.call_args[1]
        env = call_kwargs["env"]
        assert env["CODEX_HOME"] == str(tmp_path)

    def test_login_returncode_zero_returns_true(self, tmp_path, caplog):
        """returncode=0 must return True and log at INFO level."""
        import logging

        with patch("subprocess.run", return_value=_make_completed_process(returncode=0)):
            with caplog.at_level(
                logging.INFO,
                logger="code_indexer.server.startup.codex_cli_startup",
            ):
                result = _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        assert result is True
        assert any("completed successfully" in record.message for record in caplog.records)

    def test_login_nonzero_returncode_logs_warning_returns_false(self, tmp_path, caplog):
        """returncode != 0 must return False and log WARNING with truncated stderr."""
        import logging

        stderr_text = b"auth failed: invalid key"
        with patch(
            "subprocess.run",
            return_value=_make_completed_process(returncode=1, stderr=stderr_text),
        ):
            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ):
                result = _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        assert result is False
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("auth failed" in msg for msg in warning_messages)

    def test_login_timeout_returns_false(self, tmp_path, caplog):
        """TimeoutExpired must return False and log WARNING — no exception propagation."""
        import logging

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=30),
        ):
            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ):
                result = _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        assert result is False
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("timed out" in msg for msg in warning_messages)

    def test_login_codex_binary_missing_returns_false(self, tmp_path, caplog):
        """FileNotFoundError (codex not on PATH) must return False and log WARNING."""
        import logging

        with patch("subprocess.run", side_effect=FileNotFoundError("codex: not found")):
            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ):
                result = _login_codex_with_api_key(codex_home=tmp_path, api_key=TEST_API_KEY)

        assert result is False
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("not found" in msg.lower() for msg in warning_messages)

    def test_login_empty_api_key_returns_false_without_subprocess(self, tmp_path, caplog):
        """Empty api_key must return False immediately without invoking subprocess."""
        import logging

        with patch("subprocess.run") as mock_run:
            with caplog.at_level(
                logging.WARNING,
                logger="code_indexer.server.startup.codex_cli_startup",
            ):
                result = _login_codex_with_api_key(codex_home=tmp_path, api_key="")

        assert result is False
        mock_run.assert_not_called()
        warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("empty" in msg.lower() for msg in warning_messages)
