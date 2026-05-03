"""
Dispatcher-routing tests for LogScanner._invoke_claude_cli (Bug #936).

Verifies that LogScanner._invoke_claude_cli routes its LLM call through
CliDispatcher using flow="self_monitoring_scan" instead of calling
subprocess.run directly, and that it builds the dispatcher via
build_dep_map_dispatcher from the config.

Anti-mock rule: only external boundaries are patched (get_config_service,
build_dep_map_dispatcher, CodexInvoker.invoke, ClaudeInvoker.invoke).
LogScanner is never mocked or partially stubbed.

Test inventory:
  test_scanner_build_dispatcher_called_with_config
  test_scanner_dispatches_with_correct_flow_name
  test_scanner_failed_result_raises_runtime_error
  test_scanner_failover_codex_to_claude_succeeds
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)

if TYPE_CHECKING:
    from code_indexer.server.self_monitoring.scanner import LogScanner

_MODULE = "code_indexer.server.self_monitoring.scanner"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_config(
    codex_enabled: bool = False,
    codex_weight: float = 0.5,
) -> MagicMock:
    """Build a minimal mock ServerConfig."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    return cfg


def _make_success_result(
    cli_used: str = "claude", output: str = '{"status": "SUCCESS"}'
) -> InvocationResult:
    return InvocationResult(
        success=True,
        output=output,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _make_failed_result(
    error: str = "Codex timed out",
    cli_used: str = "codex",
    failure_class: FailureClass = FailureClass.RETRYABLE_ON_OTHER,
) -> InvocationResult:
    return InvocationResult(
        success=False,
        output="",
        error=error,
        cli_used=cli_used,
        was_failover=False,
        failure_class=failure_class,
    )


def _make_scanner(tmp_path: Path) -> "LogScanner":
    """Construct a minimal LogScanner for testing."""
    from code_indexer.server.self_monitoring.scanner import LogScanner

    db_path = str(tmp_path / "self_mon.db")
    log_db_path = str(tmp_path / "logs.db")

    with patch("code_indexer.server.self_monitoring.scanner.DatabaseConnectionManager"):
        return LogScanner(
            db_path=db_path,
            scan_id="test-scan-001",
            github_repo="owner/repo",
            log_db_path=log_db_path,
            prompt_template="Analyze logs: {log_db_path}\n{last_scan_log_id}\n{dedup_context}",
            repo_root=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scanner_build_dispatcher_called_with_config(tmp_path: Path):
    """
    When LogScanner._invoke_claude_cli is called, it calls
    build_dep_map_dispatcher exactly once, passing the config object
    obtained from get_config_service().get_config().
    """
    config = _make_mock_config()
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_result()

    mock_svc = MagicMock()
    mock_svc.get_config.return_value = config

    scanner = _make_scanner(tmp_path)

    with (
        patch(
            f"{_MODULE}.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_build,
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
    ):
        scanner._invoke_claude_cli("Analyze these logs.")

    mock_build.assert_called_once()
    call_args = mock_build.call_args
    config_passed = (call_args.args and call_args.args[0] is config) or (
        call_args.kwargs.get("config") is config
    )
    assert config_passed, (
        "build_dep_map_dispatcher must receive the config from get_config_service"
    )


def test_scanner_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    LogScanner._invoke_claude_cli routes through dispatcher.dispatch
    with flow='self_monitoring_scan' and does NOT call subprocess.run directly.
    """
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_result()

    mock_svc = MagicMock()
    mock_svc.get_config.return_value = _make_mock_config()

    scanner = _make_scanner(tmp_path)

    with (
        patch(
            f"{_MODULE}.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
    ):
        scanner._invoke_claude_cli("Analyze these logs.")

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by LogScanner._invoke_claude_cli"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "self_monitoring_scan", (
        f"flow must be 'self_monitoring_scan', got {call_kwargs.get('flow')!r}"
    )


def test_scanner_failed_result_raises_runtime_error(tmp_path: Path):
    """
    When dispatcher.dispatch returns a failed InvocationResult,
    LogScanner._invoke_claude_cli raises RuntimeError with the error text.
    """
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_failed_result(
        error="Codex process crashed"
    )

    mock_svc = MagicMock()
    mock_svc.get_config.return_value = _make_mock_config()

    scanner = _make_scanner(tmp_path)

    with (
        patch(
            f"{_MODULE}.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            scanner._invoke_claude_cli("Analyze these logs.")

    assert "crashed" in str(exc_info.value), (
        "RuntimeError must preserve the upstream error text"
    )


def test_scanner_failover_codex_to_claude_succeeds(tmp_path: Path):
    """
    Failover coverage: when Codex primary invoke returns RETRYABLE_ON_OTHER,
    the real CliDispatcher automatically falls back to Claude and
    _invoke_claude_cli returns the Claude output.

    Patches the concrete invokers (CodexInvoker.invoke, ClaudeInvoker.invoke)
    at the real seam so the real CliDispatcher failover logic is exercised.
    """
    from code_indexer.server.services.claude_invoker import ClaudeInvoker
    from code_indexer.server.services.codex_invoker import CodexInvoker

    codex_home = str(tmp_path / "codex-home")
    codex_config = _make_mock_config(codex_enabled=True, codex_weight=1.0)
    mock_svc = MagicMock()
    mock_svc.get_config.return_value = codex_config

    codex_calls: list = []
    claude_calls: list = []

    def _codex_invoke(self, flow, cwd, prompt, timeout, max_turns=0):
        codex_calls.append(flow)
        return _make_failed_result(
            error="Codex process failed",
            cli_used="codex",
            failure_class=FailureClass.RETRYABLE_ON_OTHER,
        )

    def _claude_invoke(self, flow, cwd, prompt, timeout, max_turns=0):
        claude_calls.append(flow)
        return _make_success_result(cli_used="claude", output='{"status": "SUCCESS"}')

    scanner = _make_scanner(tmp_path)

    with (
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
        patch.dict("os.environ", {"CODEX_HOME": codex_home}),
        patch.object(CodexInvoker, "invoke", _codex_invoke),
        patch.object(ClaudeInvoker, "invoke", _claude_invoke),
    ):
        output = scanner._invoke_claude_cli("Analyze these logs.")

    assert output == '{"status": "SUCCESS"}', (
        "_invoke_claude_cli must return the Claude fallback output"
    )
    assert len(codex_calls) == 1, "Codex primary must be invoked once before failover"
    assert len(claude_calls) == 1, (
        "Claude fallback must be invoked once after Codex RETRYABLE_ON_OTHER"
    )
