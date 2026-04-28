"""
Unit tests for CliDispatcher wiring into DependencyMapAnalyzer Pass 1 (Bug #936).

Verifies that run_pass_1_synthesis routes its invocation through CliDispatcher
(via _build_pass1_dispatcher) instead of calling _invoke_claude_cli directly.

Mirrors the pattern of test_dependency_map_pass2_dispatcher_wiring_848.py.

Anti-mock rule: only external boundaries are patched (subprocess.run, config-service).
The DependencyMapAnalyzer is never mocked or partially stubbed.

Test inventory (exhaustive list):
  test_pass1_builds_dispatcher_with_codex_when_enabled
  test_pass1_builds_claude_only_when_codex_disabled
  test_pass1_dispatches_with_correct_flow_name
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import DependencyMapAnalyzer
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

_DEFAULT_PASS_TIMEOUT = 600


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


def _make_pass1_success_result(cli_used: str = "codex") -> InvocationResult:
    """Build a successful InvocationResult carrying a valid pass1 JSON payload."""
    domain_payload = json.dumps(
        [
            {
                "name": "test-domain",
                "description": "a test domain",
                "participating_repos": ["repo-a"],
                "last_analyzed": "2024-01-01T00:00:00",
            }
        ]
    )
    return InvocationResult(
        success=True,
        output=domain_payload,
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _run_pass1(analyzer: DependencyMapAnalyzer, tmp_path: Path) -> list[Any]:
    """Call run_pass_1_synthesis with minimal fixtures. Returns domain list.

    Return type is list[Any] because run_pass_1_synthesis returns Any —
    the production API is not generically typed and cannot be narrowed here
    without coupling this test helper to internal implementation details.
    """
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(exist_ok=True)

    # Write a fake pass1_domains.json so the file-based output path is taken
    pass1_file = staging_dir / "pass1_domains.json"
    domains = [
        {
            "name": "test-domain",
            "description": "test",
            "participating_repos": ["repo-a"],
            "last_analyzed": "2024-01-01T00:00:00",
        }
    ]
    pass1_file.write_text(json.dumps(domains))

    result = analyzer.run_pass_1_synthesis(
        staging_dir=staging_dir,
        repo_descriptions={"repo-a": "A test repository."},
        repo_list=[{"alias": "repo-a", "clone_path": str(tmp_path / "repo-a")}],
        max_turns=5,
    )
    # Cast to list[Any] so mypy sees a concrete list return (production API returns Any).
    return list(result)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pass1_builds_dispatcher_with_codex_when_enabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is set,
    _build_pass1_dispatcher returns a CliDispatcher with both claude and codex
    invokers present and codex_weight=1.0.
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
        dispatcher = analyzer._build_pass1_dispatcher()

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when codex enabled and CODEX_HOME is set"
    )
    assert dispatcher.codex_weight == 1.0


def test_pass1_builds_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, _build_pass1_dispatcher
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
        dispatcher = analyzer._build_pass1_dispatcher()

    assert dispatcher.claude is not None
    assert dispatcher.codex is None, "codex must be None when codex disabled"
    assert dispatcher.codex_weight == 0.0


def test_pass1_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    run_pass_1_synthesis calls dispatcher.dispatch with flow='dependency_map_pass_1'.

    The injected mock dispatcher short-circuits subprocess spawning.
    subprocess.run (the external subprocess boundary used by _invoke_claude_cli)
    is patched and asserted NOT called, proving that the raw CLI path is bypassed
    when the dispatcher is active — without mocking any SUT method.
    """
    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_pass1_success_result(cli_used="codex")

    analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

    with patch("subprocess.run") as mock_subproc:
        _run_pass1(analyzer, tmp_path)

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by run_pass_1_synthesis"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "dependency_map_pass_1", (
        f"flow must be 'dependency_map_pass_1', got {call_kwargs.get('flow')!r}"
    )
    mock_subproc.assert_not_called()
