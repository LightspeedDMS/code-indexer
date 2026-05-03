"""
Unit tests for CliDispatcher wiring into DependencyMapAnalyzer verification pass (Bug #936).

Verifies that invoke_verification_pass routes through CliDispatcher
instead of calling _invoke_claude_cli directly, and that
_build_verification_dispatcher correctly constructs dispatchers based on config.

Mirrors the pattern of test_dependency_map_analyzer_delta_merge_dispatcher.py.

Anti-mock rule: only external boundaries are patched (subprocess.run, config-service,
prompt loading). The DependencyMapAnalyzer is never mocked or partially stubbed.

Test inventory (exhaustive list):
  test_verification_builds_dispatcher_with_codex_when_enabled
  test_verification_builds_claude_only_when_codex_disabled
  test_verification_dispatches_with_correct_flow_name
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

_DEFAULT_PASS_TIMEOUT = 600
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


def _make_verification_config() -> MagicMock:
    """Build the duck-typed config object used by invoke_verification_pass."""
    cfg = MagicMock()
    cfg.fact_check_timeout_seconds = _DEFAULT_PASS_TIMEOUT
    cfg.dependency_map_delta_max_turns = 5
    return cfg


def _make_analyzer(tmp_path: Path, cli_dispatcher=None) -> DependencyMapAnalyzer:
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=_DEFAULT_PASS_TIMEOUT,
        cli_dispatcher=cli_dispatcher,
    )


def _make_verification_success_result(
    document_path: Path,
    cli_used: str = "codex",
) -> InvocationResult:
    """Build a successful InvocationResult that passes all postconditions.

    Writes updated content + FILE_EDIT_COMPLETE to the document file so
    _check_verification_postconditions passes (non-empty file + sentinel as last line).
    """
    document_path.write_text(
        document_path.read_text(encoding="utf-8")
        + "\n## Verified\n\nAll claims confirmed.\n"
        + _COMPLETION_SIGNAL
        + "\n",
        encoding="utf-8",
    )
    return InvocationResult(
        success=True,
        output=_COMPLETION_SIGNAL,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _run_verification_pass(
    analyzer: DependencyMapAnalyzer,
    document_path: Path,
) -> None:
    """Call invoke_verification_pass with minimal fixtures."""
    repo_list = [
        {"alias": "repo-a", "clone_path": str(document_path.parent / "repo-a")}
    ]
    verification_config = _make_verification_config()
    analyzer.invoke_verification_pass(
        document_path=document_path,
        repo_list=repo_list,
        config=verification_config,
    )


# ---------------------------------------------------------------------------
# Tests: dispatcher construction (_build_verification_dispatcher)
# ---------------------------------------------------------------------------


def test_verification_builds_dispatcher_with_codex_when_enabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is set,
    _build_verification_dispatcher returns a CliDispatcher with both claude
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
        dispatcher = analyzer._build_verification_dispatcher()

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when codex enabled and CODEX_HOME is set"
    )
    assert dispatcher.codex_weight == 1.0


def test_verification_builds_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, _build_verification_dispatcher
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
        dispatcher = analyzer._build_verification_dispatcher()

    assert dispatcher.claude is not None
    assert dispatcher.codex is None, "codex must be None when codex disabled"
    assert dispatcher.codex_weight == 0.0


# ---------------------------------------------------------------------------
# Tests: dispatcher invocation behaviour (invoke_verification_pass)
# ---------------------------------------------------------------------------


def test_verification_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    invoke_verification_pass calls dispatcher.dispatch with
    flow='dependency_map_verification' and does NOT call subprocess.run directly.

    The test:
    - Creates a real domain document file.
    - Injects a mock dispatcher whose dispatch() side-effect writes updated content
      + FILE_EDIT_COMPLETE sentinel to satisfy all postconditions.
    - Patches get_prompt to return a minimal fact-check template.
    - Asserts dispatcher.dispatch was called with the correct flow name.
    - Asserts subprocess.run was NOT called (proving the dispatcher path was taken).
    """
    # Create a real domain document on disk
    document_path = tmp_path / "test-domain.md"
    document_path.write_text(
        "# Domain Analysis: test-domain\n\n## Overview\n\nThis domain has repo-a.\n",
        encoding="utf-8",
    )

    mock_dispatcher = MagicMock()

    def _dispatch_side_effect(flow, cwd, prompt, timeout):
        return _make_verification_success_result(document_path, cli_used="codex")

    mock_dispatcher.dispatch.side_effect = _dispatch_side_effect

    analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

    with (
        patch("subprocess.run") as mock_subproc,
        patch(
            "code_indexer.global_repos.prompts.get_prompt",
            return_value="Verify dependencies. Repos:\n{repo_list}\n",
        ),
    ):
        _run_verification_pass(analyzer, document_path)

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by invoke_verification_pass"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "dependency_map_verification", (
        f"flow must be 'dependency_map_verification', got {call_kwargs.get('flow')!r}"
    )
    mock_subproc.assert_not_called()
