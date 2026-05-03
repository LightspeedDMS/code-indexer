"""
Unit tests for CliDispatcher wiring into DependencyMapAnalyzer new-domain generation (Bug #936).

Verifies that invoke_new_domain_generation routes through CliDispatcher
instead of calling _invoke_claude_cli directly, and that _build_new_domain_dispatcher
correctly constructs Claude-only or Claude+Codex dispatchers based on config.

Mirrors the pattern of test_dependency_map_analyzer_pass3_dispatcher.py.

Anti-mock rule: only external boundaries are patched (subprocess.run, config-service).
The DependencyMapAnalyzer is never mocked or partially stubbed.

Test inventory (exhaustive list):
  test_new_domain_builds_dispatcher_with_codex_when_enabled
  test_new_domain_builds_claude_only_when_codex_disabled
  test_new_domain_dispatches_with_correct_flow_name
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

_DEFAULT_PASS_TIMEOUT = 600
_DOMAIN_NAME = "new-test-domain"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_config(
    codex_enabled: bool = False,
    codex_weight: float = 0.5,
):
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


def _make_analyzer(tmp_path: Path, cli_dispatcher=None) -> DependencyMapAnalyzer:
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=_DEFAULT_PASS_TIMEOUT,
        cli_dispatcher=cli_dispatcher,
    )


def _make_new_domain_success_result(cli_used: str = "codex") -> InvocationResult:
    """Build a successful InvocationResult with plausible new-domain output."""
    output = (
        "# Domain Analysis: new-test-domain\n\n## Overview\n\nThis is a new domain.\n"
    )
    return InvocationResult(
        success=True,
        output=output,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _run_new_domain_generation(analyzer: DependencyMapAnalyzer) -> str:
    """Call invoke_new_domain_generation with minimal fixtures."""
    prompt = f"Generate a new domain document for {_DOMAIN_NAME}.\n"
    return str(
        analyzer.invoke_new_domain_generation(
            prompt=prompt,
            timeout=_DEFAULT_PASS_TIMEOUT,
            max_turns=5,
        )
    )


# ---------------------------------------------------------------------------
# Tests: dispatcher construction (_build_new_domain_dispatcher)
# ---------------------------------------------------------------------------


def test_new_domain_builds_dispatcher_with_codex_when_enabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is set,
    _build_new_domain_dispatcher returns a CliDispatcher with both claude
    and codex invokers and codex_weight=1.0.
    """
    config = _make_mock_config(codex_enabled=True, codex_weight=1.0)
    codex_home = str(tmp_path / "codex-home")

    with (
        patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg,
        patch.dict("os.environ", {"CODEX_HOME": codex_home}),
    ):
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg.return_value = mock_svc

        analyzer = _make_analyzer(tmp_path)
        dispatcher = analyzer._build_new_domain_dispatcher()

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when codex enabled and CODEX_HOME is set"
    )
    assert dispatcher.codex_weight == 1.0


def test_new_domain_builds_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, _build_new_domain_dispatcher
    returns a Claude-only dispatcher (codex=None, effective weight=0.0).
    """
    config = _make_mock_config(codex_enabled=False)

    with patch(
        "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
    ) as mock_get_cfg:
        mock_svc = MagicMock()
        mock_svc.get_config.return_value = config
        mock_get_cfg.return_value = mock_svc

        analyzer = _make_analyzer(tmp_path)
        dispatcher = analyzer._build_new_domain_dispatcher()

    assert dispatcher.claude is not None
    assert dispatcher.codex is None, "codex must be None when codex disabled"
    assert dispatcher.codex_weight == 0.0


# ---------------------------------------------------------------------------
# Tests: dispatcher invocation behaviour (invoke_new_domain_generation)
# ---------------------------------------------------------------------------


def test_new_domain_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    invoke_new_domain_generation calls dispatcher.dispatch with
    flow='dependency_map_new_domain'.

    The injected mock dispatcher short-circuits subprocess spawning.
    subprocess.run is patched and asserted NOT called, proving the raw CLI path
    is bypassed when the dispatcher is active.
    """
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_new_domain_success_result(
        cli_used="codex"
    )

    analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

    with patch("subprocess.run") as mock_subproc:
        _run_new_domain_generation(analyzer)

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by invoke_new_domain_generation"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "dependency_map_new_domain", (
        f"flow must be 'dependency_map_new_domain', got {call_kwargs.get('flow')!r}"
    )
    mock_subproc.assert_not_called()
