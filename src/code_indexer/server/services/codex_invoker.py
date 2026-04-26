"""
CodexInvoker: IntelligenceCliInvoker implementation for Codex CLI.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

Invokes `codex exec --json --skip-git-repo-check
--dangerously-bypass-approvals-and-sandbox "<prompt>"` with CODEX_HOME env var set,
parses JSONL output for the last item.completed agent_message event,
and classifies failures as RETRYABLE_ON_SAME (timeout) or
RETRYABLE_ON_OTHER (auth, quota, missing output, non-zero returncode).

codex-cli 0.125+ requires the 'exec' subcommand for non-interactive mode.
The legacy `codex --json -q` form is rejected by 0.125+.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
from typing import List, Optional, Tuple, Union

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

logger = logging.getLogger(__name__)

_CLI_USED = "codex"
_STDERR_SNIPPET_LEN = 200  # max chars of stderr included in error messages
_JSONL_LOG_SNIPPET_LEN = 80  # max chars of a malformed JSONL line in caller logs

# Stderr substrings that indicate permanent credential/config problems.
# All are mapped to RETRYABLE_ON_OTHER (failover to alternate CLI).
_PERMANENT_FAILURE_STDERR_PATTERNS = (
    "authentication failed",
    "invalid api key",
    "quota exceeded",
    "configuration error",
    "unauthorized",
)


def _make_failure_result(
    error_msg: str, failure_class: FailureClass
) -> InvocationResult:
    """Construct a failed InvocationResult with was_failover=False."""
    return InvocationResult(
        success=False,
        output="",
        error=error_msg,
        cli_used=_CLI_USED,
        was_failover=False,
        failure_class=failure_class,
    )


class CodexInvoker:
    """
    Invokes the Codex CLI and parses its JSONL --json output.

    Command: CODEX_HOME=<codex_home> codex exec --json --skip-git-repo-check
             --dangerously-bypass-approvals-and-sandbox "<prompt>"

    Process isolation: Popen with start_new_session=True puts the subprocess
    into its own process group. On timeout, os.killpg kills the entire group
    (Codex spawns Node + Rust subprocesses; proc.kill() alone leaks them).

    Resource cleanup: proc.communicate(timeout=...) is used on both the normal
    and timeout paths to drain stdout/stderr pipes and reap the subprocess,
    preventing zombie processes and leaked file descriptors.
    """

    def __init__(self, codex_home: str) -> None:
        """
        Args:
            codex_home: Value to inject as CODEX_HOME. Must be non-empty string.

        Raises:
            ValueError: if codex_home is empty or not a string.
        """
        if not isinstance(codex_home, str) or not codex_home:
            raise ValueError(
                f"CodexInvoker: codex_home must be a non-empty string, got {codex_home!r}"
            )
        self._codex_home = codex_home

    def invoke(
        self, flow: str, cwd: str, prompt: str, timeout: int
    ) -> InvocationResult:
        """
        Invoke Codex CLI and return the parsed result.

        Orchestration only: validate → start → communicate → interpret.

        Args:
            flow:    Logical flow name (e.g. "describe", "refine"). Informational;
                     not passed to the subprocess. Must be a non-empty string.
            cwd:     Working directory for the subprocess. Must be non-empty.
            prompt:  Prompt text to pass to codex. Must be non-empty.
            timeout: Maximum seconds to wait; must be > 0.
        """
        validation_error = self._validate_inputs(flow, cwd, prompt, timeout)
        if validation_error is not None:
            return validation_error

        start_result = self._start_process(prompt, cwd)
        if isinstance(start_result, InvocationResult):
            return start_result
        proc = start_result

        comm_result = self._communicate_with_timeout(proc, timeout)
        if isinstance(comm_result, InvocationResult):
            return comm_result
        stdout_text, stderr_text = comm_result

        if proc.returncode != 0:
            return self._handle_nonzero_exit(proc.returncode, stderr_text)

        return self._interpret_stdout(stdout_text)

    def _validate_inputs(
        self, flow: str, cwd: str, prompt: str, timeout: int
    ) -> Optional[InvocationResult]:
        """Validate all invoke() parameters. Returns a failure result on error, else None."""
        checks = [
            (timeout <= 0, f"timeout {timeout!r}: must be > 0"),
            (
                not isinstance(flow, str) or not flow,
                f"flow must be non-empty string, got {flow!r}",
            ),
            (
                not isinstance(cwd, str) or not cwd,
                f"cwd must be non-empty string, got {cwd!r}",
            ),
            (
                not isinstance(prompt, str) or not prompt,
                f"prompt must be non-empty string, got {prompt!r}",
            ),
        ]
        for is_invalid, detail in checks:
            if is_invalid:
                error_msg = f"CodexInvoker: invalid input — {detail}"
                logger.error(error_msg)
                return _make_failure_result(error_msg, FailureClass.RETRYABLE_ON_OTHER)
        return None

    def _start_process(
        self, prompt: str, cwd: str
    ) -> "Union[subprocess.Popen[str], InvocationResult]":
        """Start the Codex subprocess. Returns Popen on success, InvocationResult on error."""
        cmd = [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            prompt,
        ]
        env = {**os.environ, "CODEX_HOME": self._codex_home}
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
                text=True,
            )
        except Exception as exc:
            error_msg = f"CodexInvoker: failed to start process: {exc}"
            logger.error(error_msg, exc_info=True)
            return _make_failure_result(error_msg, FailureClass.RETRYABLE_ON_OTHER)

    def _communicate_with_timeout(
        self, proc: "subprocess.Popen[str]", timeout: int
    ) -> "Union[Tuple[str, str], InvocationResult]":
        """
        Call proc.communicate(timeout=timeout) with cleanup on TimeoutExpired.

        On timeout: kill the entire process group (NOT proc.kill()), then drain
        pipes via a second proc.communicate() to reap the zombie.

        Returns (stdout, stderr) tuple or InvocationResult(RETRYABLE_ON_SAME).
        """
        try:
            return proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            error_msg = (
                f"CodexInvoker: timed out after {timeout}s — killing process group"
            )
            logger.warning(error_msg)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception as kill_exc:
                logger.warning("CodexInvoker: killpg failed: %s", kill_exc)
            try:
                proc.communicate()
            except Exception as drain_exc:
                logger.debug(
                    "CodexInvoker: post-kill drain failed (benign): %s", drain_exc
                )
            return _make_failure_result(error_msg, FailureClass.RETRYABLE_ON_SAME)

    def _handle_nonzero_exit(
        self, returncode: int, stderr_text: str
    ) -> InvocationResult:
        """Classify and return a failure result for a non-zero process exit."""
        failure_class = _classify_stderr_failure(stderr_text)
        error_msg = (
            f"CodexInvoker: non-zero exit {returncode}"
            f" (stderr={stderr_text[:_STDERR_SNIPPET_LEN]})"
        )
        logger.warning(
            "CodexInvoker: non-zero exit %d classified as %s",
            returncode,
            failure_class.value,
        )
        return _make_failure_result(error_msg, failure_class)

    def _interpret_stdout(self, stdout_text: str) -> InvocationResult:
        """Parse JSONL stdout and build the final success or failure result."""
        last_agent_text, malformed_lines = _parse_last_agent_message(stdout_text)
        for bad_line in malformed_lines:
            logger.debug(
                "CodexInvoker: skipped malformed JSONL line: %r",
                bad_line[:_JSONL_LOG_SNIPPET_LEN],
            )
        if last_agent_text is not None:
            return InvocationResult(
                success=True,
                output=last_agent_text,
                error="",
                cli_used=_CLI_USED,
                was_failover=False,
            )
        error_msg = (
            "CodexInvoker: no item.completed agent_message in output"
            f" (stdout_len={len(stdout_text)})"
        )
        logger.warning(error_msg)
        return _make_failure_result(error_msg, FailureClass.RETRYABLE_ON_OTHER)


# ---------------------------------------------------------------------------
# Module-level pure functions (data-in / data-out, no logging, no I/O)
# ---------------------------------------------------------------------------


def _classify_stderr_failure(stderr: str) -> FailureClass:
    """
    Pure function: classify a non-zero exit failure based on stderr content.

    All non-timeout exits map to RETRYABLE_ON_OTHER. Callers log diagnostics.
    Only timeout/SIGKILL paths (TimeoutExpired) produce RETRYABLE_ON_SAME.
    """
    stderr_lower = stderr.lower()
    for pattern in _PERMANENT_FAILURE_STDERR_PATTERNS:
        if pattern in stderr_lower:
            return FailureClass.RETRYABLE_ON_OTHER
    return FailureClass.RETRYABLE_ON_OTHER


def _parse_last_agent_message(jsonl_text: str) -> Tuple[Optional[str], List[str]]:
    """
    Pure function: parse JSONL output from `codex exec --json`.

    Returns the text of the last item.completed + agent_message event, plus
    a list of raw lines that failed json.loads(). Callers log the malformed lines.

    Returns:
        (last_agent_text, malformed_lines)
    """
    last_text: Optional[str] = None
    malformed_lines: List[str] = []

    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines.append(line)
            continue

        if not isinstance(event, dict):
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            last_text = text

    return last_text, malformed_lines
