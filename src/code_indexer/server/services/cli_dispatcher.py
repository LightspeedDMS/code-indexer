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

import random
from typing import Optional

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    IntelligenceCliInvoker,
    InvocationResult,
)


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
        self, flow: str, cwd: str, prompt: str, timeout: int
    ) -> InvocationResult:
        """
        Invoke the primary CLI and failover to the alternate if needed.

        Args:
            flow:    Logical flow name forwarded to the invoker.
            cwd:     Working directory forwarded to the invoker.
            prompt:  Prompt text forwarded to the invoker.
            timeout: Hard timeout seconds forwarded to the invoker.

        Returns:
            InvocationResult from whichever invoker ultimately ran.
        """
        # When codex is absent, bypass selection entirely.
        if self.codex is None:
            return self.claude.invoke(
                flow=flow, cwd=cwd, prompt=prompt, timeout=timeout
            )

        codex_primary = random.random() < self.codex_weight
        primary, alternate = (
            (self.codex, self.claude) if codex_primary else (self.claude, self.codex)
        )

        # First attempt on primary.
        result = primary.invoke(flow=flow, cwd=cwd, prompt=prompt, timeout=timeout)
        if result.success:
            return result

        # Single retry on RETRYABLE_ON_SAME before failing over.
        if result.failure_class == FailureClass.RETRYABLE_ON_SAME:
            retry = primary.invoke(flow=flow, cwd=cwd, prompt=prompt, timeout=timeout)
            if retry.success:
                return retry
            # Carry the retry's error forward into the failover path.
            result = retry

        # Failover to alternate (RETRYABLE_ON_OTHER OR exhausted RETRYABLE_ON_SAME).
        primary_error = f"primary={result.cli_used}: {result.error}"
        failover = alternate.invoke(flow=flow, cwd=cwd, prompt=prompt, timeout=timeout)
        failover.was_failover = True
        if not failover.success:
            failover.error = (
                f"{primary_error} | failover={failover.cli_used}: {failover.error}"
            )
        return failover
