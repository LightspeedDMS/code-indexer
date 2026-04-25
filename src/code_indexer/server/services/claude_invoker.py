"""
ClaudeInvoker: IntelligenceCliInvoker implementation for Claude CLI.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

Extracts the PTY-via-``script`` invocation pattern from
description_refresh_scheduler.py into the IntelligenceCliInvoker protocol.

Command structure:
    script -q -c "timeout <soft> claude --model <model> -p <prompt>
                  --print --dangerously-skip-permissions" /dev/null

Environment sanitization (preserved from description_refresh_scheduler.py):
    - CLAUDECODE is always stripped (prevents nested-session errors).
    - ANTHROPIC_API_KEY is stripped only when CLAUDECODE was present
      (avoids breaking API-key auth for users not running nested sessions).
    - NO_COLOR=1 is always injected to suppress colour output.

Numeric parameter contracts:
    - soft_timeout_seconds: type must be exactly int (not bool), value > 0.
    - timeout (invoke):     same strict contract; booleans are rejected.
      Uses type(x) is int because bool is a subclass of int in Python.

Failure classification:
    RETRYABLE_ON_SAME  -- subprocess.TimeoutExpired, ConnectionError, OSError
    RETRYABLE_ON_OTHER -- invalid inputs, non-zero returncode, unexpected exceptions
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from typing import Mapping, Optional

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

logger = logging.getLogger(__name__)

_CLI_USED = "claude"
_DEFAULT_SOFT_TIMEOUT_SECONDS = 90
_STDERR_SNIPPET_LEN = 200


# ---------------------------------------------------------------------------
# Module-level pure helpers (explicit data-in / data-out, no hidden state)
# ---------------------------------------------------------------------------


def _build_claude_command(prompt: str, analysis_model: str, soft_timeout: int) -> list:
    """
    Build the shell command list for invoking Claude CLI via ``script``.

    Wraps the Claude CLI in ``script -q -c ... /dev/null`` to provide a
    pseudo-TTY required for Claude CLI in non-interactive environments.

    Args:
        prompt:         Prompt string to pass to Claude.
        analysis_model: Model name (e.g. "opus", "sonnet").
        soft_timeout:   Inner shell timeout budget in seconds.

    Returns:
        Command list suitable for ``subprocess.run``.
    """
    claude_cmd = (
        f"timeout {soft_timeout}"
        f" claude --model {shlex.quote(analysis_model)}"
        f" -p {shlex.quote(prompt)}"
        f" --print --dangerously-skip-permissions"
    )
    return ["script", "-q", "-c", claude_cmd, os.devnull]


def _build_claude_env(source_env: Mapping[str, str]) -> dict:
    """
    Build a sanitised subprocess environment from source_env.

    Pure function: all state comes from the explicit ``source_env`` argument.
    Callers pass ``os.environ`` at the subprocess boundary.

    Strips CLAUDECODE always; strips ANTHROPIC_API_KEY only when CLAUDECODE
    was present (preserves original description_refresh_scheduler.py behavior).
    Injects NO_COLOR=1 to suppress colour output from the script wrapper.

    Args:
        source_env: Mapping to copy and sanitise (typically os.environ).

    Returns:
        Dict of environment variables for the subprocess.
    """
    keys_to_strip = {"CLAUDECODE"}
    if "CLAUDECODE" in source_env:
        keys_to_strip.add("ANTHROPIC_API_KEY")
    filtered = {k: v for k, v in source_env.items() if k not in keys_to_strip}
    filtered["NO_COLOR"] = "1"
    return filtered


def _normalize_claude_output(raw: str) -> str:
    """
    Strip terminal control sequences and normalise line endings from stdout.

    Removes CSI, OSC and bare ESC sequences emitted by the ``script`` wrapper,
    normalises CR/LF line endings, and trims chain-of-thought text before
    the opening ``---`` YAML frontmatter delimiter.

    Args:
        raw: Raw stdout string from the subprocess.

    Returns:
        Cleaned string ready for further parsing.
    """
    output = raw
    # CSI sequences: ECMA-48 grammar
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    # OSC sequences: ESC ] ... BEL or ESC ] ... ST
    output = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?", "", output)
    # Other ESC sequences (ESC followed by single char)
    output = re.sub(r"\x1b[^[\]()]", "", output)
    # Stray control artifacts from script command
    output = re.sub(r"\[<u", "", output)
    # Strip any remaining bare ESC bytes
    output = output.replace("\x1b", "")
    # Normalize line endings
    output = output.replace("\r\n", "\n").replace("\r", "")
    output = output.strip()
    # Strip chain-of-thought text before YAML frontmatter
    frontmatter_match = re.search(r"^---\s*$", output, re.MULTILINE)
    if frontmatter_match and frontmatter_match.start() > 0:
        output = output[frontmatter_match.start():]
    return output


def _make_failure(error_msg: str, failure_class: FailureClass) -> InvocationResult:
    """Construct a failed InvocationResult with was_failover=False."""
    return InvocationResult(
        success=False,
        output="",
        error=error_msg,
        cli_used=_CLI_USED,
        was_failover=False,
        failure_class=failure_class,
    )


# ---------------------------------------------------------------------------
# ClaudeInvoker
# ---------------------------------------------------------------------------


class ClaudeInvoker:
    """
    Invokes the Claude CLI via ``script`` (PTY wrapper) and normalises output.

    Implements the IntelligenceCliInvoker protocol so it can be composed
    with CodexInvoker inside CliDispatcher.

    The ``script`` utility wraps the subprocess in a pseudo-TTY, which is
    required for Claude CLI to run in non-interactive environments (CI, daemons).
    """

    def __init__(
        self,
        analysis_model: str = "opus",
        soft_timeout_seconds: int = _DEFAULT_SOFT_TIMEOUT_SECONDS,
    ) -> None:
        """
        Args:
            analysis_model:       Claude model name passed to --model. Must be
                                  a non-empty string. Defaults to "opus".
            soft_timeout_seconds: Inner shell timeout budget. Must be exactly
                                  int (not bool) and > 0. Defaults to 90.

        Raises:
            ValueError: if analysis_model is not a non-empty str, or
                        if soft_timeout_seconds is not exactly int > 0.
        """
        if not isinstance(analysis_model, str) or not analysis_model:
            raise ValueError(
                f"ClaudeInvoker: analysis_model must be a non-empty string, "
                f"got {analysis_model!r}"
            )
        # type(x) is int rejects bool (bool is a subclass of int, not int itself)
        if type(soft_timeout_seconds) is not int or soft_timeout_seconds <= 0:
            raise ValueError(
                f"ClaudeInvoker: soft_timeout_seconds must be int > 0, "
                f"got {soft_timeout_seconds!r}"
            )
        self._analysis_model = analysis_model
        self._soft_timeout_seconds = soft_timeout_seconds

    def invoke(self, flow: str, cwd: str, prompt: str, timeout: int) -> InvocationResult:
        """
        Invoke the Claude CLI and return a normalised result.

        Validates all inputs before touching the subprocess so callers always
        receive a well-typed InvocationResult, never a raw exception.

        Args:
            flow:    Logical flow name (informational; not passed to subprocess).
                     Must be non-empty string.
            cwd:     Working directory. Must be non-empty string.
            prompt:  Prompt text. Must be non-empty string.
            timeout: Hard timeout seconds for subprocess.run.
                     Must be exactly int (not bool) and > 0.

        Returns:
            InvocationResult with all fields set appropriately.
        """
        validation_error = self._validate_inputs(flow, cwd, prompt, timeout)
        if validation_error is not None:
            return validation_error

        cmd = _build_claude_command(prompt, self._analysis_model, self._soft_timeout_seconds)
        env = _build_claude_env(os.environ)

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            error_msg = f"ClaudeInvoker: timed out after {timeout}s"
            logger.warning(error_msg)
            return _make_failure(error_msg, FailureClass.RETRYABLE_ON_SAME)
        except (ConnectionError, OSError) as exc:
            error_msg = f"ClaudeInvoker: network/OS error: {exc}"
            logger.warning(error_msg)
            return _make_failure(error_msg, FailureClass.RETRYABLE_ON_SAME)
        except Exception as exc:
            error_msg = f"ClaudeInvoker: unexpected error: {exc}"
            logger.error(error_msg, exc_info=True)
            return _make_failure(error_msg, FailureClass.RETRYABLE_ON_OTHER)

        if result.returncode != 0:
            error_msg = (
                f"ClaudeInvoker: non-zero exit {result.returncode}"
                f" (stderr={result.stderr[:_STDERR_SNIPPET_LEN]})"
            )
            logger.warning(error_msg)
            return _make_failure(error_msg, FailureClass.RETRYABLE_ON_OTHER)

        normalized = _normalize_claude_output(result.stdout)
        return InvocationResult(
            success=True,
            output=normalized,
            error="",
            cli_used=_CLI_USED,
            was_failover=False,
        )

    def _validate_inputs(
        self, flow: str, cwd: str, prompt: str, timeout: int
    ) -> Optional[InvocationResult]:
        """
        Validate invoke() parameters before starting the subprocess.

        Uses type(x) is int (not isinstance) to reject booleans for timeout,
        then checks range. Returns RETRYABLE_ON_OTHER on first violation, else None.
        """
        checks = [
            (
                type(timeout) is not int or timeout <= 0,
                f"timeout {timeout!r}: must be int > 0",
            ),
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
                error_msg = f"ClaudeInvoker: invalid input — {detail}"
                logger.error(error_msg)
                return _make_failure(error_msg, FailureClass.RETRYABLE_ON_OTHER)
        return None
