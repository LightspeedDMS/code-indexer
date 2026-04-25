"""
Tests for ClaudeInvoker (Story #847).

ClaudeInvoker wraps the PTY-via-script invocation pattern extracted from
description_refresh_scheduler.py into the IntelligenceCliInvoker protocol.

All subprocess calls are mocked via unittest.mock.patch.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    IntelligenceCliInvoker,
    InvocationResult,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_invoker(analysis_model: str = "opus", soft_timeout_seconds: int = 90) -> "ClaudeInvoker":
    """Import here so import errors surface as test failures, not collection errors."""
    from code_indexer.server.services.claude_invoker import ClaudeInvoker

    return ClaudeInvoker(analysis_model=analysis_model, soft_timeout_seconds=soft_timeout_seconds)


def _completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess."""
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


class TestClaudeInvokerConstructor:
    def test_constructor_accepts_analysis_model(self):
        """ClaudeInvoker.__init__ stores analysis_model without raising."""
        invoker = _make_invoker(analysis_model="opus")
        assert invoker is not None

    def test_constructor_default_model(self):
        """Default analysis_model is 'opus'."""
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        invoker = ClaudeInvoker()
        assert invoker._analysis_model == "opus"

    def test_constructor_stores_soft_timeout(self):
        """Constructor stores soft_timeout_seconds and uses it in command."""
        invoker = _make_invoker(soft_timeout_seconds=45)
        assert invoker._soft_timeout_seconds == 45

    def test_satisfies_protocol(self):
        """ClaudeInvoker satisfies IntelligenceCliInvoker protocol at runtime."""
        invoker = _make_invoker()
        assert isinstance(invoker, IntelligenceCliInvoker)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestClaudeInvokerInputValidation:
    def test_empty_prompt_returns_failure_without_subprocess(self):
        """Empty prompt → RETRYABLE_ON_OTHER failure; subprocess never called."""
        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()

    def test_empty_cwd_returns_failure_without_subprocess(self):
        """Empty cwd → RETRYABLE_ON_OTHER failure; subprocess never called."""
        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="", prompt="some prompt", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()

    def test_zero_timeout_returns_failure_without_subprocess(self):
        """timeout <= 0 → RETRYABLE_ON_OTHER failure; subprocess never called."""
        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=0)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()

    def test_negative_timeout_returns_failure_without_subprocess(self):
        """Negative timeout → RETRYABLE_ON_OTHER failure; subprocess never called."""
        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=-5)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()

    def test_bool_timeout_rejected_as_not_int(self):
        """bool timeout (True/False) is rejected — bool is not a valid int here."""
        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=True)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()

    def test_bool_soft_timeout_raises_in_constructor(self):
        """Passing bool as soft_timeout_seconds raises ValueError in constructor."""
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        with pytest.raises(ValueError, match="soft_timeout_seconds"):
            ClaudeInvoker(soft_timeout_seconds=True)

    def test_empty_analysis_model_raises_in_constructor(self):
        """Empty analysis_model raises ValueError in constructor."""
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        with pytest.raises(ValueError, match="analysis_model"):
            ClaudeInvoker(analysis_model="")

    def test_none_analysis_model_raises_in_constructor(self):
        """None analysis_model raises ValueError in constructor.

        The type: ignore below is intentional — this test verifies runtime
        validation catches invalid values that the type system cannot prevent
        at call sites using dynamic/untyped code paths.
        """
        from code_indexer.server.services.claude_invoker import ClaudeInvoker

        with pytest.raises(ValueError, match="analysis_model"):
            ClaudeInvoker(analysis_model=None)  # type: ignore[arg-type]  # testing runtime guard

    def test_empty_flow_returns_failure_without_subprocess(self):
        """Empty flow → RETRYABLE_ON_OTHER failure; subprocess never called."""
        import os

        with patch("subprocess.run") as mock_run:
            invoker = _make_invoker()
            # os.devnull is a portable, guaranteed-present path on all Unix systems
            result = invoker.invoke(flow="", cwd=os.devnull, prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Command-line construction
# ---------------------------------------------------------------------------


class TestClaudeInvokerCommandLine:
    def test_command_uses_script_wrapper(self):
        """Command list starts with 'script'."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="hello world")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/tmp", prompt="describe this", timeout=30)
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "script"

    def test_command_includes_claude_binary(self):
        """Command string passed to script contains 'claude'."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="output")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/tmp", prompt="my prompt", timeout=30)
            cmd = mock_run.call_args[0][0]
            # The claude command is embedded as the -c argument to script
            claude_cmd_str = " ".join(cmd)
            assert "claude" in claude_cmd_str

    def test_command_includes_model_flag(self):
        """Command string contains --model with the configured model name."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="output")
            invoker = _make_invoker(analysis_model="sonnet")
            invoker.invoke(flow="describe", cwd="/tmp", prompt="test", timeout=30)
            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(cmd)
            assert "--model" in cmd_str
            assert "sonnet" in cmd_str

    def test_command_includes_prompt_text(self):
        """The prompt is included in the command passed to script."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="result text")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/tmp", prompt="unique-prompt-xyz", timeout=30)
            cmd = mock_run.call_args[0][0]
            # prompt is shell-quoted inside the -c argument
            cmd_str = " ".join(cmd)
            assert "unique-prompt-xyz" in cmd_str

    def test_command_includes_print_flag(self):
        """Claude CLI is called with --print flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="output")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/tmp", prompt="test", timeout=30)
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "--print" in cmd_str

    def test_soft_timeout_embedded_in_command(self):
        """The soft_timeout_seconds value appears in the shell command string."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="output")
            invoker = _make_invoker(soft_timeout_seconds=45)
            invoker.invoke(flow="describe", cwd="/tmp", prompt="test", timeout=120)
            cmd_str = " ".join(mock_run.call_args[0][0])
            assert "45" in cmd_str


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestClaudeInvokerSuccessPath:
    def test_success_returns_invocation_result(self):
        """Subprocess success → InvocationResult(success=True)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="description text")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert isinstance(result, InvocationResult)
            assert result.success is True

    def test_success_output_contains_normalized_stdout(self):
        """On success, result.output is the normalized stdout text."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="clean output text")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert "clean output text" in result.output

    def test_success_cli_used_is_claude(self):
        """On success, result.cli_used is 'claude'."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="text")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.cli_used == "claude"

    def test_success_was_failover_false(self):
        """On success, result.was_failover is False."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="text")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.was_failover is False

    def test_ansi_escape_sequences_stripped_from_output(self):
        """Terminal escape sequences in stdout are stripped before returning."""
        raw = "\x1b[32mhello world\x1b[0m"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout=raw)
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert "\x1b" not in result.output
            assert "hello world" in result.output


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestClaudeInvokerFailureClassification:
    def test_nonzero_returncode_returns_retryable_on_other(self):
        """Non-zero returncode → FailureClass.RETRYABLE_ON_OTHER."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(returncode=1, stderr="error")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER

    def test_timeout_expired_returns_retryable_on_same(self):
        """subprocess.TimeoutExpired → FailureClass.RETRYABLE_ON_SAME."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_SAME
            assert result.cli_used == "claude"

    def test_connection_error_returns_retryable_on_same(self):
        """ConnectionError on subprocess.run → FailureClass.RETRYABLE_ON_SAME."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = ConnectionError("network unreachable")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_SAME

    def test_oserror_returns_retryable_on_same(self):
        """OSError (e.g. EHOSTUNREACH) on subprocess.run → FailureClass.RETRYABLE_ON_SAME."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = OSError("host unreachable")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_SAME

    def test_generic_exception_returns_retryable_on_other(self):
        """Unexpected non-network exceptions → FailureClass.RETRYABLE_ON_OTHER."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("unexpected internal error")
            invoker = _make_invoker()
            result = invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
            assert result.success is False
            assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER


# ---------------------------------------------------------------------------
# Env and subprocess parameter passthrough
# ---------------------------------------------------------------------------


class TestClaudeInvokerSubprocessParams:
    def test_cwd_passed_to_subprocess_run(self):
        """The cwd parameter is forwarded to subprocess.run."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="text")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/repo/myproject", prompt="p", timeout=30)
            _, kwargs = mock_run.call_args
            assert kwargs.get("cwd") == "/repo/myproject"

    def test_timeout_passed_to_subprocess_run(self):
        """The timeout parameter is forwarded to subprocess.run."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="text")
            invoker = _make_invoker()
            invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=77)
            _, kwargs = mock_run.call_args
            assert kwargs.get("timeout") == 77

    def test_env_ordinary_vars_forwarded_and_claudecode_stripped(self):
        """Ordinary env vars are forwarded to subprocess.run; CLAUDECODE is stripped."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _completed_process(stdout="text")
            with patch.dict(
                "os.environ",
                {"CLAUDECODE": "1", "MY_CUSTOM_VAR": "my_value"},
                clear=False,
            ):
                invoker = _make_invoker()
                invoker.invoke(flow="describe", cwd="/tmp", prompt="p", timeout=30)
                _, kwargs = mock_run.call_args
                env = kwargs.get("env", {})
                # Positive passthrough: ordinary var must be present
                assert env.get("MY_CUSTOM_VAR") == "my_value"
                # Negative: CLAUDECODE must be stripped
                assert "CLAUDECODE" not in env
