"""
Shared CliDispatcher factory for dep-map LLM invocations (Bug #936).

Provides build_dep_map_dispatcher(config) as the single source of truth for
building a CliDispatcher that respects codex_integration_config.codex_weight.

This helper is used by ALL dispatcher builders across the codebase:
  - dependency_map_service.py (run_graph_repair_dry_run + run_full_analysis)
  - dependency_map_routes.py (_build_repair_executor)
  - dependency_map_analyzer.py (_build_pass1_dispatcher, _build_pass2_dispatcher,
    _build_pass3_dispatcher)
  - description_refresh_scheduler.py (_build_cli_dispatcher)

Pattern: ClaudeInvoker is always constructed. When
codex_integration_config.enabled=True AND CODEX_HOME is in os.environ, a
CodexInvoker is also constructed and wired in with the configured codex_weight.
When codex is unavailable (disabled or CODEX_HOME missing), codex=None and the
CliDispatcher collapses the effective weight to 0.0 (Claude-only routing).
"""

from __future__ import annotations

import os
from typing import Optional

from code_indexer.server.services.cli_dispatcher import CliDispatcher
from code_indexer.server.services.claude_invoker import ClaudeInvoker
from code_indexer.server.services.codex_invoker import CodexInvoker
from code_indexer.server.services.codex_mcp_auth_header_provider import (
    build_codex_mcp_auth_header_provider,
)


def build_dep_map_dispatcher(
    config,
    analysis_model: str = "opus",
    claude_soft_timeout_seconds: Optional[int] = None,
) -> CliDispatcher:
    """
    Build a CliDispatcher for dep-map LLM invocations from *config*.

    This is the single source of truth for CliDispatcher construction across all
    callers. All per-caller builder methods (_build_cli_dispatcher,
    _build_pass1_dispatcher, _build_pass2_dispatcher, _build_pass3_dispatcher)
    must delegate here rather than duplicating construction logic inline.

    Constructs a ClaudeInvoker unconditionally. When
    config.codex_integration_config.enabled is True and CODEX_HOME is set
    in os.environ, also constructs a CodexInvoker and wires it in with the
    weight from config. Otherwise codex=None and the effective weight
    collapses to 0.0 inside CliDispatcher.

    Args:
        config: ServerConfig returned by get_config_service().get_config().
                May be None when called from contexts where config is unavailable
                (e.g., first-boot before DB init); in that case Claude-only mode
                is used.
        analysis_model: Claude model name passed to ClaudeInvoker (default "opus").
        claude_soft_timeout_seconds: Optional inner shell timeout budget forwarded
                to ClaudeInvoker. When None, derived from
                ``config.dependency_map_pass_timeout_seconds`` if available;
                otherwise ClaudeInvoker's own default applies. Must be a
                positive int when provided directly (ClaudeInvoker enforces this).

    Returns:
        A fully initialised CliDispatcher.
    """
    # Bug #1049 Option B: factory is the single point of truth for timeout.
    # Explicit caller override is forwarded as-is (ClaudeInvoker validates it
    # and raises ValueError for bad values — do NOT swallow those errors here).
    # When no explicit override, derive from config.dependency_map_pass_timeout_seconds
    # if the attribute exists and holds a valid positive int; otherwise let
    # ClaudeInvoker use its own built-in default.
    if claude_soft_timeout_seconds is not None:
        # Explicit override: forward unconditionally so ClaudeInvoker can enforce
        # its own type/range contract (raises ValueError on 0, bool, float, etc.)
        claude_invoker = ClaudeInvoker(
            analysis_model=analysis_model,
            soft_timeout_seconds=claude_soft_timeout_seconds,
        )
    else:
        config_timeout = (
            getattr(config, "dependency_map_pass_timeout_seconds", None)
            if config is not None
            else None
        )
        if (
            config_timeout is not None
            and isinstance(config_timeout, int)
            and not isinstance(config_timeout, bool)
            and config_timeout > 0
        ):
            claude_invoker = ClaudeInvoker(
                analysis_model=analysis_model,
                soft_timeout_seconds=config_timeout,
            )
        else:
            claude_invoker = ClaudeInvoker(analysis_model=analysis_model)

    codex_invoker = None
    codex_weight = 0.0
    codex_cfg = config.codex_integration_config if config else None
    if codex_cfg and codex_cfg.enabled:
        codex_home = os.environ.get("CODEX_HOME", "")
        if codex_home:
            codex_invoker = CodexInvoker(
                codex_home=codex_home,
                auth_header_provider=build_codex_mcp_auth_header_provider(),
            )
            codex_weight = codex_cfg.codex_weight

    return CliDispatcher(
        claude=claude_invoker,
        codex=codex_invoker,
        codex_weight=codex_weight,
    )
