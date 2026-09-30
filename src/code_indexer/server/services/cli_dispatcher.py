"""
CliDispatcher: weighted selection + retry/failover for IntelligenceCliInvoker.

Story #847: CLI Dispatcher (Selection + Failover) for Description Gen + Refinement.

Selects a primary invoker (Claude or Codex) based on a configurable weight,
retries once on the same invoker for RETRYABLE_ON_SAME failures, and fails
over to the alternate invoker for all other failures.

Failover policy:
    1. Pick primary: random.random() < codex_weight → Codex; else Claude.
    2. Invoke primary.
    3. If success → return.
    4. If RETRYABLE_ON_SAME → retry once on primary.
       If retry succeeds → return (was_failover=False).
       If retry also fails → fall through to step 5 with the retry's error.
    5. Failover to alternate (was_failover=True).
    6. If alternate fails → append primary error to alternate error and return.

When codex is None, the effective weight is always 0.0 (Claude only, no selection).
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from code_indexer.server.services.claude_invoker import ClaudeInvoker
from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    IntelligenceCliInvoker,
    InvocationResult,
)

logger = logging.getLogger(__name__)

# Flows in this set carry a safety model that lives entirely inside the
# concrete ClaudeInvoker class (its restricted --allowedTools/--permission-
# mode policy in _build_claude_command), NOT in the generic
# IntelligenceCliInvoker protocol. A deployment-supplied CIDX_CLI_INVOKER
# plugin (see cli_invoker_plugin.py) implements only that protocol and
# carries none of it. No plugin ships in this repo or is installed by
# scripts/install-cidx-server.sh or the auto-updater today, but the
# mechanism is real and reachable via an env var, so dispatch() refuses to
# run these flows through anything that is not the known, restricted
# ClaudeInvoker -- fail closed rather than silently trusting an unrecognized
# invoker with the log database and Bash.
_FLOWS_REQUIRING_KNOWN_INVOKER = frozenset({"self_monitoring_scan"})

# Flows in this set run on Claude only: Codex never receives them, at any
# codex_weight, and a Claude failure on one of them never fails over to
# Codex. This is an owner decision distinct from the invoker-identity guard
# above (which governs what "Claude" must be for these flows); the same
# flow name lives in both sets today, but the two checks are independent.
_CLAUDE_ONLY_FLOWS = frozenset({"self_monitoring_scan"})


class CliDispatcher:
    """
    Dispatches CLI invocations to Claude or Codex with weighted selection,
    single retry on RETRYABLE_ON_SAME, and automatic failover.
    """

    def __init__(
        self,
        claude: IntelligenceCliInvoker,
        codex: Optional[IntelligenceCliInvoker] = None,
        codex_weight: float = 0.5,
    ) -> None:
        """
        Args:
            claude:       Claude invoker (required; always available as fallback).
            codex:        Codex invoker (optional; if None, all dispatches go to Claude).
            codex_weight: Probability [0.0, 1.0] that Codex is chosen as primary.
                          Ignored when codex is None.

        Raises:
            ValueError: if codex_weight is not in [0.0, 1.0].
        """
        if not 0.0 <= codex_weight <= 1.0:
            raise ValueError(
                f"codex_weight must be in [0.0, 1.0], got {codex_weight!r}"
            )
        self.claude = claude
        self.codex = codex
        # Effective weight is 0.0 when codex is unavailable so the selection
        # branch never activates and all dispatches go straight to Claude.
        self.codex_weight = codex_weight if codex is not None else 0.0

    def dispatch(
        self, flow: str, cwd: str, prompt: str, timeout: int, max_turns: int = 0
    ) -> InvocationResult:
        """
        Invoke the primary CLI and failover to the alternate if needed.

        Args:
            flow:      Logical flow name forwarded to the invoker.
            cwd:       Working directory forwarded to the invoker.
            prompt:    Prompt text forwarded to the invoker.
            timeout:   Hard timeout seconds forwarded to the invoker.
            max_turns: When > 0, forwarded as ``--max-turns`` to enable agentic
                       mode.  Must be int (not bool) and >= 0.
                       Default 0 = single-shot mode.

        Raises:
            ValueError: If max_turns is not an int or is negative.

        Returns:
            InvocationResult from whichever invoker ultimately ran.
        """
        if (
            not isinstance(max_turns, int)
            or isinstance(max_turns, bool)
            or max_turns < 0
        ):
            raise ValueError(
                f"CliDispatcher.dispatch: max_turns must be int >= 0, got {max_turns!r}"
            )

        if flow in _FLOWS_REQUIRING_KNOWN_INVOKER and not isinstance(
            self.claude, ClaudeInvoker
        ):
            error_msg = (
                f"CliDispatcher: refusing to run flow {flow!r} through a "
                "non-standard CLI invoker (e.g. a CIDX_CLI_INVOKER plugin) -- "
                "this flow's safety depends on ClaudeInvoker's own restricted "
                "permission policy, which a plugin invoker does not carry. "
                "Failing closed rather than running it unrestricted."
            )
            logger.error(error_msg)
            return InvocationResult(
                success=False,
                output="",
                error=error_msg,
                cli_used="none",
                was_failover=False,
                failure_class=FailureClass.RETRYABLE_ON_OTHER,
            )

        # Owner decision: these flows run on Claude only. Codex never
        # receives them regardless of codex_weight, and a Claude failure
        # never fails over to Codex -- only the same-invoker retry applies
        # (at most one retry, on RETRYABLE_ON_SAME, same as the general
        # failover path below).
        if flow in _CLAUDE_ONLY_FLOWS:
            for _attempt in range(2):
                result = self.claude.invoke(
                    flow=flow,
                    cwd=cwd,
                    prompt=prompt,
                    timeout=timeout,
                    max_turns=max_turns,
                )
                if (
                    result.success
                    or result.failure_class != FailureClass.RETRYABLE_ON_SAME
                ):
                    return result
            return result

        # When codex is absent, bypass selection entirely.
        if self.codex is None:
            return self.claude.invoke(
                flow=flow,
                cwd=cwd,
                prompt=prompt,
                timeout=timeout,
                max_turns=max_turns,
            )

        codex_primary = random.random() < self.codex_weight
        primary, alternate = (
            (self.codex, self.claude) if codex_primary else (self.claude, self.codex)
        )

        # First attempt on primary.
        result = primary.invoke(
            flow=flow, cwd=cwd, prompt=prompt, timeout=timeout, max_turns=max_turns
        )
        if result.success:
            return result

        # Single retry on RETRYABLE_ON_SAME before failing over.
        if result.failure_class == FailureClass.RETRYABLE_ON_SAME:
            retry = primary.invoke(
                flow=flow,
                cwd=cwd,
                prompt=prompt,
                timeout=timeout,
                max_turns=max_turns,
            )
            if retry.success:
                return retry
            # Carry the retry's error forward into the failover path.
            result = retry

        # Failover to alternate (RETRYABLE_ON_OTHER OR exhausted RETRYABLE_ON_SAME).
        primary_error = f"primary={result.cli_used}: {result.error}"
        failover = alternate.invoke(
            flow=flow, cwd=cwd, prompt=prompt, timeout=timeout, max_turns=max_turns
        )
        failover.was_failover = True
        if not failover.success:
            failover.error = (
                f"{primary_error} | failover={failover.cli_used}: {failover.error}"
            )
        return failover
