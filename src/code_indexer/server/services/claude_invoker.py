"""
ClaudeInvoker: IntelligenceCliInvoker implementation for Claude CLI.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

Extracts the PTY-via-``script`` invocation pattern from
description_refresh_scheduler.py into the IntelligenceCliInvoker protocol.

Command structure:
    script -q -e -c "timeout <soft> claude --model <model> -p <prompt>
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

from code_indexer.server.services.agent_cli_isolation import (
    build_claude_isolation_args,
    build_self_monitoring_claude_args,
    is_isolation_exempt_flow,
    prepare_stable_neutral_cwd,
    remove_mcp_config_file,
    try_build_mcp_config_file,
)
from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)
from code_indexer.server.services.pace_maker_guard import (  # Story #997
    enforce_pace_maker_config,
)
from code_indexer.server.self_monitoring import log_query

logger = logging.getLogger(__name__)

_CLI_USED = "claude"
_DEFAULT_SOFT_TIMEOUT_SECONDS = 1800
_STDERR_SNIPPET_LEN = 200
# The one isolation-exempt flow that gets its OWN narrow
# permission policy (build_self_monitoring_claude_args) instead of
# --dangerously-skip-permissions. Kept as a literal-string constant here,
# mirroring agent_cli_isolation._ISOLATION_EXEMPT_FLOWS, so a future SECOND
# exempt flow (should one ever be added there) does not silently inherit
# this self-monitoring-specific policy by accident.
_SELF_MONITORING_SCAN_FLOW = "self_monitoring_scan"


# ---------------------------------------------------------------------------
# Module-level pure helpers (explicit data-in / data-out, no hidden state)
# ---------------------------------------------------------------------------


def _build_claude_command(
    prompt: str,
    analysis_model: str,
    soft_timeout: int,
    max_turns: int = 0,
    *,
    flow: str = "",
    analysis_dir: str = "",
    mcp_config_path: Optional[str] = None,
) -> list:
    """
    Build the shell command list for invoking Claude CLI via ``script``.

    Wraps the Claude CLI in ``script -q -c ... /dev/null`` to provide a
    pseudo-TTY required for Claude CLI in non-interactive environments.

    For every flow except the narrow isolation-exempt set (see
    ``agent_cli_isolation.is_isolation_exempt_flow``), the agent keeps its
    full built-in tool set (Bash, Read, Write, Edit, Glob, Grep) and runs
    with ``--dangerously-skip-permissions`` (needed for those tools to run
    non-interactively) plus the isolation flags from
    ``build_claude_isolation_args`` — a neutral working directory (via
    --add-dir) and an explicit MCP server list.

    The one flow in that exempt set, ``self_monitoring_scan``, does NOT
    keep the pre-existing --dangerously-skip-permissions shape: its log-DB
    content can carry request-supplied text, so this flow instead gets the
    narrow policy from ``build_self_monitoring_claude_args``: an
    --allowedTools entry pinned to the server-owned log-query entry point
    (see agent_cli_isolation.build_self_monitoring_log_query_command_prefix),
    a --settings entry that fences file reads (including Claude Code's
    built-in read-only Bash commands) to the working directory in every
    permission mode, a "dontAsk" --permission-mode (auto-denies anything
    else instead of prompting or hanging), and --strict-mcp-config with
    zero MCP servers. The log database path is NOT part of this command at
    all: it reaches the log-query entry point only via the
    CIDX_LOG_QUERY_DB_PATH environment variable, which invoke() sets on the
    subprocess -- see _build_claude_env.

    Args:
        prompt:          Prompt string to pass to Claude.
        analysis_model:  Model name (e.g. "opus", "sonnet").
        soft_timeout:    Inner shell timeout budget in seconds.
        max_turns:       When > 0, adds ``--max-turns`` flag to enable
                         agentic mode. When 0 (default), single-shot
                         ``--print`` mode.
        flow:            Logical flow name; selects the isolation policy.
        analysis_dir:    Directory the agent needs read AND write access to
                         (passed via --add-dir for non-exempt flows).
        mcp_config_path: Path to a standalone --mcp-config file naming only
                         the cidx-local server, or None when no such file
                         could be built (the invocation still runs with
                         --strict-mcp-config alone in that case).

    Returns:
        Command list suitable for ``subprocess.run``.
    """
    max_turns_flag = f" --max-turns {max_turns}" if max_turns > 0 else ""
    base_cmd = (
        f"timeout {soft_timeout}"
        f" claude --model {shlex.quote(analysis_model)}"
        f" -p {shlex.quote(prompt)}"
        f"{max_turns_flag}"
        f" --print"
    )
    if flow == _SELF_MONITORING_SCAN_FLOW:
        self_mon_args = build_self_monitoring_claude_args()
        quoted_self_mon_args = " ".join(shlex.quote(arg) for arg in self_mon_args)
        claude_cmd = f"{base_cmd} {quoted_self_mon_args}"
    elif is_isolation_exempt_flow(flow):
        claude_cmd = base_cmd + " --dangerously-skip-permissions"
    else:
        iso_args = build_claude_isolation_args(analysis_dir, mcp_config_path)
        quoted_iso_args = " ".join(shlex.quote(arg) for arg in iso_args)
        claude_cmd = f"{base_cmd} {quoted_iso_args}"
    return ["script", "-q", "-e", "-c", claude_cmd, os.devnull]


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

    Removes CSI, OSC and bare ESC sequences emitted by the ``script`` wrapper
    and normalises CR/LF line endings.

    Bug #1369: this function used to also strip everything before the first
    ``^---$`` line, on the theory that Claude's response was YAML-frontmatter
    markdown and any text before the frontmatter was discardable
    chain-of-thought. That heuristic had zero live consumers (verified: the
    live description-generation flow — flow="repo_lifecycle" via
    LifecycleClaudeCliInvoker — parses a strict JSON payload via
    UnifiedResponseParser, and description_refresh_scheduler.py's own copy of
    this same heuristic was never called from anywhere in that module) and it
    silently destroyed the JSON payload for flow="self_monitoring_scan"
    whenever Claude's trailing prose happened to contain a markdown
    horizontal-rule / section-separator ``---`` line. Do not reintroduce
    unscoped ``---``-based trimming here — it is dead weight for every current
    caller and actively harmful for JSON-response flows.

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
        log_db_path: Optional[str] = None,
    ) -> None:
        """
        Args:
            analysis_model:       Claude model name passed to --model. Must be
                                  a non-empty string. Defaults to "opus".
            soft_timeout_seconds: Inner shell timeout budget. Must be exactly
                                  int (not bool) and > 0. Defaults to 90.
            log_db_path:          Path to the server's log database. Only
                                  consumed for flow == "self_monitoring_scan"
                                  : set as the CIDX_LOG_QUERY_DB_PATH
                                  environment variable for that subprocess.
                                  Every other flow ignores it entirely. Optional
                                  so every other caller (dep-map passes,
                                  lifecycle description generation, ...) is
                                  unaffected.

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
        self._log_db_path = log_db_path

    def invoke(
        self, flow: str, cwd: str, prompt: str, timeout: int, max_turns: int = 0
    ) -> InvocationResult:
        """
        Invoke the Claude CLI and return a normalised result.

        Validates all inputs before touching the subprocess so callers always
        receive a well-typed InvocationResult, never a raw exception.

        Args:
            flow:      Logical flow name (informational; not passed to subprocess).
                       Must be non-empty string.
            cwd:       Working directory. Must be non-empty string.
            prompt:    Prompt text. Must be non-empty string.
            timeout:   Hard timeout seconds for subprocess.run.
                       Must be exactly int (not bool) and > 0.
            max_turns: When > 0, passed as ``--max-turns`` to Claude CLI to
                       enable agentic mode.  Must be int (not bool) and >= 0.
                       Default 0 = single-shot mode.

        Returns:
            InvocationResult with all fields set appropriately.
        """
        # Story #997: Enforce pace-maker config before Claude CLI invocation (non-fatal)
        try:
            enforce_pace_maker_config()
        except Exception as exc:
            logger.debug("enforce_pace_maker_config failed (non-fatal): %s", exc)

        validation_error = self._validate_inputs(flow, cwd, prompt, timeout)
        if validation_error is not None:
            return validation_error

        if (
            not isinstance(max_turns, int)
            or isinstance(max_turns, bool)
            or max_turns < 0
        ):
            error_msg = f"ClaudeInvoker: max_turns must be int >= 0, got {max_turns!r}"
            return _make_failure(error_msg, FailureClass.NOT_RETRYABLE)

        exempt = is_isolation_exempt_flow(flow)
        subprocess_cwd = cwd
        scratch_dir: Optional[str] = None
        mcp_config_path: Optional[str] = None
        if not exempt:
            # The analyzed directory (cwd, as the caller understands it) is
            # never the subprocess's own working directory — a repository-
            # authored CLAUDE.md/AGENTS.md there must not be auto-loaded as
            # trusted CLI configuration. It is still reachable by the agent
            # via --add-dir (built into the command below). The scratch
            # directory is STABLE per target (cwd) rather than fresh per
            # call, so the claude CLI's own per-cwd session-transcript
            # folder is created once per target instead of once per call.
            scratch_dir = prepare_stable_neutral_cwd(cwd)
            subprocess_cwd = scratch_dir
            mcp_config_path = try_build_mcp_config_file()

        if flow == _SELF_MONITORING_SCAN_FLOW and not self._log_db_path:
            # Never fall back to an unrestricted invocation
            # just because the caller forgot log_db_path -- fail loud so
            # the gap is visible immediately, not discovered later as an
            # obscure "env var not set" failure deep inside the log-query
            # entry point.
            error_msg = (
                "ClaudeInvoker: flow 'self_monitoring_scan' requires "
                "log_db_path (construct ClaudeInvoker with log_db_path=...) "
                "-- refusing to run this flow without it"
            )
            logger.error(error_msg)
            return _make_failure(error_msg, FailureClass.RETRYABLE_ON_OTHER)

        cmd = _build_claude_command(
            prompt,
            self._analysis_model,
            self._soft_timeout_seconds,
            max_turns,
            flow=flow,
            analysis_dir=cwd,
            mcp_config_path=mcp_config_path,
        )
        env = _build_claude_env(os.environ)
        if flow == _SELF_MONITORING_SCAN_FLOW:
            # The log-query entry point takes the database path ONLY via
            # this environment variable -- never a CLI argument, so the
            # agent cannot redirect it elsewhere.
            env[log_query.ENV_VAR_DB_PATH] = self._log_db_path

        try:
            result = subprocess.run(
                cmd,
                cwd=subprocess_cwd,
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
        finally:
            # scratch_dir is a stable per-target directory (see
            # prepare_stable_neutral_cwd) and is intentionally NOT removed
            # here -- it is emptied at the START of the next call for the
            # same target instead, so the claude CLI's per-cwd session-
            # transcript folder is created once per target, not once per
            # call.
            remove_mcp_config_file(mcp_config_path)

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
