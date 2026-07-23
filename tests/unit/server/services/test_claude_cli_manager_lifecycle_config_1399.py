"""
Bug #1399 CRITICAL item 4 (call site 1/2): ClaudeCliManager._process_work's
CliDispatcher-routed path constructs a fresh, default-valued
LifecycleAnalysisConfig() instead of reading the saved config from
ConfigService -- so a Web UI change to lifecycle_analysis.outer_timeout_seconds
/ shell_timeout_seconds never reaches this call site (divergent consumer;
the primary LifecycleClaudeCliInvoker path already reads fresh config
correctly -- see test_lifecycle_claude_cli_invoker_config.py).

Fix: read timeout via get_config_service().get_config().lifecycle_analysis_config
at call time, mirroring the correct reference implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

if TYPE_CHECKING:
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

# Deliberately distinct from LifecycleAnalysisConfig's defaults (1800/1860)
# so a false-pass via defaults is impossible.
_CONFIGURED_OUTER_TIMEOUT_SECONDS = 650


def _make_success_result(output: str = "generated description") -> InvocationResult:
    return InvocationResult(
        success=True,
        output=output,
        error="",
        cli_used="claude",
        was_failover=False,
    )


def _make_manager(cli_dispatcher) -> "ClaudeCliManager":
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

    return ClaudeCliManager(api_key=None, max_workers=0, cli_dispatcher=cli_dispatcher)


class TestProcessWorkReadsConfiguredLifecycleTimeout:
    def test_process_work_dispatch_uses_config_service_outer_timeout(
        self, tmp_path: Path
    ) -> None:
        """
        Bug #1399: _process_work's dispatcher.dispatch(timeout=...) must use
        get_config_service().get_config().lifecycle_analysis_config.outer_timeout_seconds,
        not a fresh LifecycleAnalysisConfig() default (1860s).
        """
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(str(tmp_path))
        service.update_setting(
            "lifecycle_analysis",
            "outer_timeout_seconds",
            _CONFIGURED_OUTER_TIMEOUT_SECONDS,
        )

        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result()

        manager = _make_manager(cli_dispatcher=mock_dispatcher)

        received: list = []

        def callback(success, result):
            received.append((success, result))

        with patch(
            "code_indexer.server.services.claude_cli_manager.get_config_service",
            return_value=service,
        ):
            manager._process_work(tmp_path, callback)

        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        assert call_kwargs.get("timeout") == _CONFIGURED_OUTER_TIMEOUT_SECONDS, (
            "Bug #1399: ClaudeCliManager._process_work must read "
            "outer_timeout_seconds from ConfigService at call time, not "
            f"construct a fresh LifecycleAnalysisConfig() default; got "
            f"{call_kwargs.get('timeout')!r}."
        )
