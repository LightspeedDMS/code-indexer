"""
Unit tests for CodexInvoker (Story #847).

All subprocess calls are mocked — no real Codex or Claude CLI is invoked.

Covers:
- JSONL parsing: single item.completed agent_message extracted correctly
- JSONL parsing: only the LAST item.completed agent_message returned
- JSONL parsing: no matching events → failure with RETRYABLE_ON_OTHER
- JSONL parsing: malformed JSON line skipped gracefully
- JSONL parsing: item.completed with item.type != agent_message ignored
- Process-group kill on timeout (os.killpg called, proc.kill NOT called)
- Timeout failure classified as RETRYABLE_ON_SAME
- Subprocess command-line includes --json -q and prompt; CODEX_HOME env set
- Auth failure in stderr → RETRYABLE_ON_OTHER
- Quota error in stderr → RETRYABLE_ON_OTHER
- Network/timeout → RETRYABLE_ON_SAME
- Nonzero returncode without agent_message → RETRYABLE_ON_OTHER
"""

from __future__ import annotations

import json
import signal
import subprocess
from typing import List, Optional, Tuple
from unittest.mock import MagicMock, patch

from code_indexer.server.services.intelligence_cli_invoker import FailureClass
from code_indexer.server.services.codex_invoker import CodexInvoker


# ---------------------------------------------------------------------------
# Test constants — all repeated literals live here
# ---------------------------------------------------------------------------

_FAKE_CODEX_HOME = "/fake/codex-home"
_CUSTOM_CODEX_HOME = "/custom/codex-home"
_REPO_CWD = "/repo"
_SPECIAL_CWD = "/my/special/repo"
_DEFAULT_TIMEOUT = 60
_SHORT_TIMEOUT = 5
_ONE_SECOND_TIMEOUT = 1
_FAKE_PID = 12345
_FAKE_PGID = 9000
_FLOW_DESCRIBE = "describe"
_PROMPT_DESCRIBE = "describe this"
_PROMPT_SHORT = "p"
_PROMPT_TEST = "test"
_PROMPT_SPECIAL = "my special prompt"
_CLI_USED_CODEX = "codex"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jsonl(*events: dict) -> str:
    """Build a JSONL string from event dicts."""
    return "\n".join(json.dumps(e) for e in events)


def _agent_message_event(text: str) -> dict:
    return {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }


def _other_event(event_type: str = "message.delta") -> dict:
    return {"type": event_type, "delta": {"text": "noise"}}


def _make_invoker(codex_home: str = _FAKE_CODEX_HOME) -> CodexInvoker:
    return CodexInvoker(codex_home=codex_home)


def _make_proc(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    pid: int = _FAKE_PID,
    communicate_side_effect: Optional[List] = None,
) -> MagicMock:
    """Build a mock Popen object.

    When communicate_side_effect is provided (a list of return values or
    exceptions), proc.communicate.side_effect is set — useful for simulating
    TimeoutExpired on the first call followed by a drain on the second call.

    When communicate_side_effect is None, proc.communicate.return_value is set
    to (stdout, stderr) for the normal completion path.
    """
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    if communicate_side_effect is not None:
        proc.communicate.side_effect = communicate_side_effect
    else:
        proc.communicate.return_value = (stdout, stderr)
    return proc


def _make_success_proc(text: str = "output text", pid: int = _FAKE_PID) -> MagicMock:
    """Build a mock Popen object that returns a single agent_message event."""
    return _make_proc(
        stdout=json.dumps(_agent_message_event(text)),
        stderr="",
        returncode=0,
        pid=pid,
    )


# ---------------------------------------------------------------------------
# JSONL parsing tests
# ---------------------------------------------------------------------------


class TestCodexInvokerJsonlParsing:
    def test_single_agent_message_event_extracted(self) -> None:
        """Single item.completed agent_message → its text is the output."""
        invoker = _make_invoker()
        jsonl = _jsonl(
            _other_event("session.start"),
            _agent_message_event("description of repo"),
            _other_event("session.end"),
        )
        proc = _make_proc(stdout=jsonl)
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_DESCRIBE,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is True
        assert result.output == "description of repo"
        assert result.cli_used == _CLI_USED_CODEX

    def test_last_agent_message_event_returned(self) -> None:
        """Multiple item.completed agent_message events → only the LAST is returned."""
        invoker = _make_invoker()
        jsonl = _jsonl(
            _agent_message_event("first message"),
            _other_event("tool.call"),
            _agent_message_event("second message"),
            _agent_message_event("final answer"),
        )
        proc = _make_proc(stdout=jsonl)
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_DESCRIBE,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is True
        assert result.output == "final answer"

    def test_no_agent_message_event_returns_failure(self) -> None:
        """No item.completed agent_message events → failure with RETRYABLE_ON_OTHER."""
        invoker = _make_invoker()
        jsonl = _jsonl(
            _other_event("session.start"),
            _other_event("message.delta"),
        )
        proc = _make_proc(stdout=jsonl)
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_DESCRIBE,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
        assert result.output == ""

    def test_malformed_json_line_skipped_gracefully(self) -> None:
        """Malformed JSON line in JSONL stream is skipped; valid events still parsed."""
        invoker = _make_invoker()
        lines = [
            json.dumps(_other_event("session.start")),
            "this is not valid json {{{",
            json.dumps(_agent_message_event("real answer")),
        ]
        proc = _make_proc(stdout="\n".join(lines))
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_DESCRIBE,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is True
        assert result.output == "real answer"

    def test_item_completed_but_not_agent_message_ignored(self) -> None:
        """item.completed events with item.type != agent_message are not returned."""
        invoker = _make_invoker()
        jsonl = _jsonl(
            {"type": "item.completed", "item": {"type": "tool_call", "text": "tool output"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
        )
        proc = _make_proc(stdout=jsonl)
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_DESCRIBE,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER


# ---------------------------------------------------------------------------
# Process group kill on timeout
# ---------------------------------------------------------------------------


class TestCodexInvokerTimeout:
    def test_timeout_calls_killpg_not_just_proc_kill(self) -> None:
        """On timeout, os.killpg(os.getpgid(pid), SIGKILL) must be called.
        proc.kill() must NOT be called — only the process-group kill path is used."""
        invoker = _make_invoker()
        # First communicate() raises TimeoutExpired; second drains after killpg
        proc = _make_proc(
            pid=9999,
            communicate_side_effect=[
                subprocess.TimeoutExpired(cmd=_CLI_USED_CODEX, timeout=_ONE_SECOND_TIMEOUT),
                ("", ""),
            ],
        )

        with patch("subprocess.Popen", return_value=proc):
            with patch("os.getpgid", return_value=_FAKE_PGID) as mock_getpgid:
                with patch("os.killpg") as mock_killpg:
                    result = invoker.invoke(
                        flow=_FLOW_DESCRIBE,
                        cwd=_REPO_CWD,
                        prompt=_PROMPT_TEST,
                        timeout=_ONE_SECOND_TIMEOUT,
                    )

        mock_getpgid.assert_called_once_with(9999)
        mock_killpg.assert_called_once_with(_FAKE_PGID, signal.SIGKILL)
        # Critically: proc.kill() must NOT have been called — group kill only
        proc.kill.assert_not_called()
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_SAME

    def test_timeout_result_classified_as_retryable_on_same(self) -> None:
        """Timeout (SIGKILL) is classified as RETRYABLE_ON_SAME."""
        invoker = _make_invoker()
        proc = _make_proc(
            pid=1111,
            communicate_side_effect=[
                subprocess.TimeoutExpired(cmd=_CLI_USED_CODEX, timeout=_SHORT_TIMEOUT),
                ("", ""),
            ],
        )

        with patch("subprocess.Popen", return_value=proc):
            with patch("os.getpgid", return_value=1000):
                with patch("os.killpg"):
                    result = invoker.invoke(
                        flow=_FLOW_DESCRIBE,
                        cwd=_REPO_CWD,
                        prompt=_PROMPT_TEST,
                        timeout=_SHORT_TIMEOUT,
                    )

        assert result.failure_class == FailureClass.RETRYABLE_ON_SAME
        assert result.success is False


# ---------------------------------------------------------------------------
# Subprocess command-line and environment
# ---------------------------------------------------------------------------


def _invoke_and_capture_popen(
    invoker: CodexInvoker,
    *,
    prompt: str = _PROMPT_SHORT,
    cwd: str = _REPO_CWD,
) -> "Tuple[list, dict]":
    """Invoke the invoker with a mocked Popen and return (cmd, popen_kwargs)."""
    proc = _make_success_proc()
    with patch("subprocess.Popen", return_value=proc) as mock_popen:
        invoker.invoke(
            flow=_FLOW_DESCRIBE,
            cwd=cwd,
            prompt=prompt,
            timeout=_DEFAULT_TIMEOUT,
        )
    return mock_popen.call_args[0][0], mock_popen.call_args[1]


class TestCodexInvokerSubprocessSetup:
    def test_command_uses_codex_exec_subcommand_with_json_flag(self) -> None:
        """Regression: codex-cli 0.125+ requires 'exec' subcommand, not -q.

        Full required command shape:
          ['codex', 'exec', '--json', '--skip-git-repo-check',
           '--dangerously-bypass-approvals-and-sandbox', <prompt>]
        """
        invoker = _make_invoker()
        cmd, _ = _invoke_and_capture_popen(invoker, prompt=_PROMPT_SHORT)
        assert cmd[0] == "codex"
        assert cmd[1] == "exec"
        assert "--json" in cmd
        assert "--skip-git-repo-check" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == _PROMPT_SHORT

    def test_command_includes_prompt_text(self) -> None:
        """Popen command contains the prompt string."""
        invoker = _make_invoker()
        cmd, _ = _invoke_and_capture_popen(invoker, prompt=_PROMPT_SPECIAL)
        assert any(_PROMPT_SPECIAL in str(arg) for arg in cmd)

    def test_codex_home_env_var_injected(self) -> None:
        """CODEX_HOME env var is set to the configured codex_home path."""
        invoker = _make_invoker(codex_home=_CUSTOM_CODEX_HOME)
        _, kwargs = _invoke_and_capture_popen(invoker)
        env = kwargs.get("env", {})
        assert env.get("CODEX_HOME") == _CUSTOM_CODEX_HOME

    def test_popen_called_with_start_new_session_true(self) -> None:
        """Popen is called with start_new_session=True for process-group isolation."""
        invoker = _make_invoker()
        _, kwargs = _invoke_and_capture_popen(invoker)
        assert kwargs.get("start_new_session") is True

    def test_cwd_passed_to_popen(self) -> None:
        """The cwd parameter is forwarded to Popen."""
        invoker = _make_invoker()
        _, kwargs = _invoke_and_capture_popen(invoker, cwd=_SPECIAL_CWD)
        assert kwargs.get("cwd") == _SPECIAL_CWD


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestCodexInvokerFailureClassification:
    def test_auth_failure_stderr_returns_retryable_on_other(self) -> None:
        """stderr containing auth-failure text → RETRYABLE_ON_OTHER."""
        invoker = _make_invoker()
        proc = _make_proc(
            stderr="Error: authentication failed: invalid API key", returncode=1
        )
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_SHORT,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER

    def test_quota_error_stderr_returns_retryable_on_other(self) -> None:
        """stderr containing quota error → RETRYABLE_ON_OTHER."""
        invoker = _make_invoker()
        proc = _make_proc(
            stderr="Error: quota exceeded for this account", returncode=1
        )
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_SHORT,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER

    def test_network_timeout_classified_as_retryable_on_same(self) -> None:
        """TimeoutExpired (network/process timeout) → RETRYABLE_ON_SAME."""
        invoker = _make_invoker()
        proc = _make_proc(
            pid=7777,
            communicate_side_effect=[
                subprocess.TimeoutExpired(cmd=_CLI_USED_CODEX, timeout=_DEFAULT_TIMEOUT),
                ("", ""),
            ],
        )
        with patch("subprocess.Popen", return_value=proc):
            with patch("os.getpgid", return_value=7000):
                with patch("os.killpg"):
                    result = invoker.invoke(
                        flow=_FLOW_DESCRIBE,
                        cwd=_REPO_CWD,
                        prompt=_PROMPT_SHORT,
                        timeout=_DEFAULT_TIMEOUT,
                    )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_SAME

    def test_nonzero_returncode_without_agent_message_returns_retryable_on_other(self) -> None:
        """Non-zero returncode with no agent_message events → RETRYABLE_ON_OTHER."""
        invoker = _make_invoker()
        proc = _make_proc(stderr="some generic error", returncode=2)
        with patch("subprocess.Popen", return_value=proc):
            result = invoker.invoke(
                flow=_FLOW_DESCRIBE,
                cwd=_REPO_CWD,
                prompt=_PROMPT_SHORT,
                timeout=_DEFAULT_TIMEOUT,
            )
        assert result.success is False
        assert result.failure_class == FailureClass.RETRYABLE_ON_OTHER
