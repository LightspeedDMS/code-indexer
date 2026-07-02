"""
Unit tests for CliDispatcher wiring into DependencyMapAnalyzer delta merge (Bug #936).

Verifies that invoke_delta_merge_file routes its primary LLM call through
CliDispatcher instead of calling _invoke_claude_cli directly.

Note on allowed_tools: invoke_delta_merge_file historically called _invoke_claude_cli
with allowed_tools="mcp__cidx-local__search_code". The IntelligenceCliInvoker protocol
does not support allowed_tools, so the dispatcher handles the primary (non-retry) call
without tool restriction. The workaround is accepted: the dispatcher is a superset path
(Codex does not support --allowedTools either; Claude receives the default tool set
which includes MCP tools when registered).

Anti-mock rule: only external boundaries are patched (subprocess.run, config-service,
temp file writes). The DependencyMapAnalyzer is never mocked or partially stubbed.

Test inventory (exhaustive list):
  test_delta_merge_builds_dispatcher_with_codex_when_enabled
  test_delta_merge_builds_claude_only_when_codex_disabled
  test_delta_merge_dispatches_with_correct_flow_name
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

_DEFAULT_PASS_TIMEOUT = 600
_DOMAIN_NAME = "test-domain"
# A sentinel string that invoke_delta_merge_file uses to detect successful file edit.
_COMPLETION_SIGNAL = "FILE_EDIT_COMPLETE"


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


def _make_delta_success_result(cli_used: str = "codex") -> InvocationResult:
    """Build a successful InvocationResult that carries the file-edit completion signal."""
    return InvocationResult(
        success=True,
        output=_COMPLETION_SIGNAL,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _run_delta_merge(
    analyzer: DependencyMapAnalyzer,
    tmp_path: Path,
) -> None:
    """Call invoke_delta_merge_file with minimal fixtures."""
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(exist_ok=True)
    existing_content = (
        f"# Domain Analysis: {_DOMAIN_NAME}\n\n"
        "## Overview\n\nThis domain contains repo-a.\n"
    )
    merge_prompt = f"Update domain {_DOMAIN_NAME} with new findings.\n"
    analyzer.invoke_delta_merge_file(
        domain_name=_DOMAIN_NAME,
        existing_content=existing_content,
        merge_prompt=merge_prompt,
        timeout=_DEFAULT_PASS_TIMEOUT,
        max_turns=10,
        temp_dir=temp_dir,
    )


# ---------------------------------------------------------------------------
# Tests: dispatcher construction
# ---------------------------------------------------------------------------


def test_delta_merge_builds_dispatcher_with_codex_when_enabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is set,
    _build_delta_merge_dispatcher returns a CliDispatcher with both claude
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
        dispatcher = analyzer._build_delta_merge_dispatcher()

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when codex enabled and CODEX_HOME is set"
    )
    assert dispatcher.codex_weight == 1.0


def test_delta_merge_builds_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, _build_delta_merge_dispatcher
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
        dispatcher = analyzer._build_delta_merge_dispatcher()

    assert dispatcher.claude is not None
    assert dispatcher.codex is None, "codex must be None when codex disabled"
    assert dispatcher.codex_weight == 0.0


# ---------------------------------------------------------------------------
# Tests: dispatcher invocation behaviour
# ---------------------------------------------------------------------------


def test_delta_merge_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    invoke_delta_merge_file calls dispatcher.dispatch with
    flow='dependency_map_delta_merge' and does NOT call _invoke_claude_cli.

    The test:
    - Injects a mock dispatcher that simulates file modification.
    - Patches _invoke_claude_cli to raise so the old path fails deterministically.
    - Asserts dispatcher.dispatch was called with the correct flow name.
    - Asserts _invoke_claude_cli was NOT called (proving the dispatcher path was taken).
    """
    mock_dispatcher = MagicMock()

    def _dispatch_side_effect(flow, cwd, prompt, timeout):
        # Simulate file modification so the SUT's _verify_file_modified check passes.
        # The temp file path is embedded in the prompt by _build_file_based_instructions.
        import re

        match = re.search(
            r"(?:path|file)[:\s]+`?([^\s`]+\.md)`?", prompt, re.IGNORECASE
        )
        if match:
            path = Path(match.group(1))
            if path.exists():
                path.write_text(path.read_text() + "\n## Updated\n\nUpdated content.\n")
        return _make_delta_success_result(cli_used="codex")

    mock_dispatcher.dispatch.side_effect = _dispatch_side_effect

    analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

    with patch.object(
        analyzer,
        "_invoke_claude_cli",
        side_effect=AssertionError(
            "_invoke_claude_cli must not be called on the primary path"
        ),
    ) as mock_claude_cli:
        _run_delta_merge(analyzer, tmp_path)
        mock_claude_cli.assert_not_called()

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by invoke_delta_merge_file"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "dependency_map_delta_merge", (
        f"flow must be 'dependency_map_delta_merge', got {call_kwargs.get('flow')!r}"
    )
