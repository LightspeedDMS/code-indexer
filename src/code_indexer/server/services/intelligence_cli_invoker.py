"""
IntelligenceCliInvoker protocol, InvocationResult dataclass, and FailureClass enum.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

This module defines the shared contracts used by ClaudeInvoker, CodexInvoker,
and CliDispatcher so they can be composed without coupling to concrete types.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The result types are the CLI invoker seam's, in the SDK
# (``cidx_sdk.seams.cli_invoker``), so a delegate builds them without
# importing the core; re-exported so existing imports, isinstance checks and
# enum comparisons see the same objects.
from cidx_sdk.seams.cli_invoker import (
    FailureClass as FailureClass,
    InvocationResult as InvocationResult,
)


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
