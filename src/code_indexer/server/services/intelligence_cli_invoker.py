"""
IntelligenceCliInvoker protocol, InvocationResult dataclass, and FailureClass enum.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

This module defines the shared contracts used by ClaudeInvoker, CodexInvoker,
and CliDispatcher so they can be composed without coupling to concrete types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class FailureClass(Enum):
    """
    Classification of CLI invocation failures.

    RETRYABLE_ON_SAME  - network errors, timeout transients: retry once on the same CLI.
    RETRYABLE_ON_OTHER - all other non-retryable failures: failover to the alternate CLI.
    """

    RETRYABLE_ON_SAME = "retryable_on_same"
    RETRYABLE_ON_OTHER = "retryable_on_other"


@dataclass
class InvocationResult:
    """
    Result of a single CLI invocation attempt.

    Fields:
        success:       True when the CLI produced usable output.
        output:        The usable text output from the CLI (empty on failure).
        error:         Human-readable error description (empty on success).
        cli_used:      Which CLI ran: "claude" or "codex".
        was_failover:  True if this result came from the failover CLI, not the primary.
        failure_class: Set only when success=False; classifies retry strategy.
    """

    success: bool
    output: str
    error: str
    cli_used: str
    was_failover: bool
    failure_class: Optional[FailureClass] = None


@runtime_checkable
class IntelligenceCliInvoker(Protocol):
    """
    Structural protocol for CLI invokers (ClaudeInvoker, CodexInvoker).

    Any class with invoke(flow, cwd, prompt, timeout) -> InvocationResult
    satisfies this protocol.  Marked @runtime_checkable so isinstance checks
    work for duck-type compliance verification.
    """

    def invoke(
        self, flow: str, cwd: str, prompt: str, timeout: int, max_turns: int = 0
    ) -> InvocationResult:
        """
        Invoke the CLI with the given prompt.

        Args:
            flow:      Logical flow name, e.g. "describe" or "refine".
            cwd:       Working directory for the subprocess.
            prompt:    Full prompt text to pass to the CLI.
            timeout:   Maximum seconds to wait before killing the process.
            max_turns: When > 0, enables agentic mode with that many turns.
                       Default 0 = single-shot mode.

        Returns:
            InvocationResult with success, output, error, cli_used, was_failover,
            and failure_class set appropriately.
        """
        ...
