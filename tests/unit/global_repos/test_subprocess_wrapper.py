"""
Tests for the shared parameterized subprocess wrapper (invoke_claude_cli).

Covers env filtering, output cleaning, timeout parameterization, and error handling.
Subprocess calls are mocked — no actual Claude CLI invocations occur.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# Named timeout constants — avoids magic numbers throughout tests
SHELL_TIMEOUT = 90
OUTER_TIMEOUT = 120
LONG_SHELL_TIMEOUT = 180
LONG_OUTER_TIMEOUT = 240

# The production contract requires the platform null device as the script output
# device for pseudo-TTY in non-interactive environments.
SCRIPT_NULL_DEVICE = os.devnull


def _make_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Create a mock subprocess.CompletedProcess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _run_wrapper(
    tmp_path: Path,
    env: dict,
    stdout: str = "hello world output",
    returncode: int = 0,
    side_effect=None,
    shell_timeout: int = SHELL_TIMEOUT,
    outer_timeout: int = OUTER_TIMEOUT,
):
    """
    Call invoke_claude_cli with mocked subprocess.run and patched env.

    Returns (mock_run, success, output).
    """
    from code_indexer.global_repos.repo_analyzer import invoke_claude_cli

    with patch("subprocess.run") as mock_run:
        if side_effect is not None:
            mock_run.side_effect = side_effect
        else:
            mock_run.return_value = _make_result(returncode=returncode, stdout=stdout)
        with patch.dict("os.environ", env, clear=True):
            success, output = invoke_claude_cli(
                str(tmp_path), "test prompt", shell_timeout, outer_timeout
            )
    return mock_run, success, output


class TestEnvFiltering:
    """Tests that the wrapper filters the subprocess environment correctly."""

    @pytest.mark.parametrize(
        "extra_env,absent_key",
        [
            ({"CLAUDECODE": "1"}, "CLAUDECODE"),
            ({"CLAUDECODE": "1", "ANTHROPIC_API_KEY": "sk-x"}, "ANTHROPIC_API_KEY"),
        ],
    )
    def test_sensitive_keys_dropped_when_claudecode_present(
        self, tmp_path, extra_env, absent_key
    ):
        """CLAUDECODE and (when CLAUDECODE present) ANTHROPIC_API_KEY are dropped."""
        env = {"PATH": "/usr/bin", **extra_env}
        mock_run, _, _ = _run_wrapper(tmp_path, env)
        called_env = mock_run.call_args[1]["env"]
        assert absent_key not in called_env

    def test_anthropic_api_key_kept_when_claudecode_absent(self, tmp_path):
        """ANTHROPIC_API_KEY is kept when CLAUDECODE is not in parent env."""
        env = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-secret"}
        mock_run, _, _ = _run_wrapper(tmp_path, env)
        called_env = mock_run.call_args[1]["env"]
        assert "ANTHROPIC_API_KEY" in called_env

    def test_other_env_vars_passed_through(self, tmp_path):
        """Non-filtered env vars are forwarded to subprocess."""
        env = {"MY_VAR": "hello", "ANOTHER": "world"}
        mock_run, _, _ = _run_wrapper(tmp_path, env)
        called_env = mock_run.call_args[1]["env"]
        assert called_env["MY_VAR"] == "hello"
        assert called_env["ANOTHER"] == "world"


class TestOutputCleaning:
    """Tests that control sequences are stripped from Claude output."""

    @pytest.mark.parametrize(
        "raw_stdout,absent_fragment",
        [
            ("\x1b[31mred\x1b[0m", "\x1b["),
            ("\x1b]0;title\x07normal", "\x1b]"),
            ("\x1bMtext", "\x1b"),
            ("[<uartifact", "[<u"),
            ("line\r\nnext", "\r"),
        ],
    )
    def test_control_sequences_removed(self, tmp_path, raw_stdout, absent_fragment):
        """Each type of control/escape sequence is absent from cleaned output."""
        _, success, output = _run_wrapper(tmp_path, {}, stdout=raw_stdout)
        assert success is True
        assert absent_fragment not in output

    def test_output_stripped_of_whitespace(self, tmp_path):
        """Output has leading and trailing whitespace stripped."""
        _, success, output = _run_wrapper(tmp_path, {}, stdout="  \n  result  \n  ")
        assert success is True
        assert output == "result"


class TestTimeoutAndCommand:
    """Tests that timeout values and command structure are correct."""

    def test_outer_timeout_passed_to_subprocess_run(self, tmp_path):
        """outer_timeout_seconds is passed as timeout= to subprocess.run."""
        mock_run, _, _ = _run_wrapper(tmp_path, {}, outer_timeout=LONG_OUTER_TIMEOUT)
        assert mock_run.call_args[1]["timeout"] == LONG_OUTER_TIMEOUT

    def test_inner_timeout_in_command_string(self, tmp_path):
        """shell_timeout_seconds appears in the inner claude command string."""
        mock_run, _, _ = _run_wrapper(
            tmp_path,
            {},
            shell_timeout=LONG_SHELL_TIMEOUT,
            outer_timeout=LONG_OUTER_TIMEOUT,
        )
        full_cmd = mock_run.call_args[0][0]
        inner_cmd = full_cmd[3]  # script -q -c <inner_cmd> /dev/null
        assert str(LONG_SHELL_TIMEOUT) in inner_cmd

    def test_script_pseudo_tty_wrapper_used(self, tmp_path):
        """Outer command uses 'script -q -c ... /dev/null' for pseudo-TTY."""
        mock_run, _, _ = _run_wrapper(tmp_path, {})
        full_cmd = mock_run.call_args[0][0]
        assert full_cmd[0] == "script"
        assert full_cmd[1] == "-q"
        assert full_cmd[2] == "-c"
        assert full_cmd[4] == SCRIPT_NULL_DEVICE

    def test_repo_path_used_as_cwd(self, tmp_path):
        """repo_path argument is passed as cwd= to subprocess.run."""
        mock_run, _, _ = _run_wrapper(tmp_path, {})
        assert mock_run.call_args[1]["cwd"] == str(tmp_path)


class TestErrorHandling:
    """Tests that error conditions return (False, message) without raising."""

    @pytest.mark.parametrize(
        "side_effect,returncode",
        [
            (subprocess.TimeoutExpired(cmd="claude", timeout=OUTER_TIMEOUT), 0),
            (OSError("permission denied"), 0),
        ],
    )
    def test_exception_returns_false(self, tmp_path, side_effect, returncode):
        """subprocess exceptions return (False, str) without propagating."""
        _, success, msg = _run_wrapper(tmp_path, {}, side_effect=side_effect)
        assert success is False
        assert isinstance(msg, str)

    def test_nonzero_exit_returns_false(self, tmp_path):
        """Non-zero subprocess exit code returns (False, error_message)."""
        _, success, msg = _run_wrapper(tmp_path, {}, returncode=1)
        assert success is False
        assert isinstance(msg, str)

    def test_success_returns_true_with_output(self, tmp_path):
        """Zero exit returns (True, cleaned_output)."""
        _, success, output = _run_wrapper(tmp_path, {}, stdout="clean result")
        assert success is True
        assert output == "clean result"
