"""
Dispatcher-routing tests for LifecycleClaudeCliInvoker (Bug #936).

Verifies that the invoker routes its LLM call through CliDispatcher using
flow="repo_lifecycle" instead of calling invoke_claude_cli directly, and
that it builds the dispatcher via build_dep_map_dispatcher from the config.

Anti-mock rule: only external boundaries are patched (get_config_service,
build_dep_map_dispatcher, dispatcher.dispatch). The LifecycleClaudeCliInvoker
class itself is never mocked or partially stubbed.

Test inventory:
  test_lifecycle_build_dispatcher_called_with_config
  test_lifecycle_build_dispatcher_claude_only_when_codex_disabled
  test_lifecycle_build_dispatcher_codex_when_enabled
  test_lifecycle_dispatches_with_correct_flow_name
  test_lifecycle_dispatcher_failure_raises_runtime_error
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


# ---------------------------------------------------------------------------
# Shared setup helpers
# ---------------------------------------------------------------------------


def _make_mock_config(
    codex_enabled: bool = False,
    codex_weight: float = 0.5,
) -> MagicMock:
    """Build a minimal mock ServerConfig with lifecycle and codex sub-configs."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    lifecycle_cfg = MagicMock()
    lifecycle_cfg.shell_timeout_seconds = 360
    lifecycle_cfg.outer_timeout_seconds = 420

    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    cfg.lifecycle_analysis_config = lifecycle_cfg
    return cfg


@contextmanager
def _mock_config_svc(config: MagicMock):
    """Patch get_config_service in the lifecycle invoker module."""
    mock_svc = MagicMock()
    mock_svc.get_config.return_value = config
    with patch(
        "code_indexer.global_repos.lifecycle_claude_cli_invoker.get_config_service",
        return_value=mock_svc,
    ):
        yield mock_svc


def _make_success_invocation_result() -> InvocationResult:
    """Return a successful InvocationResult carrying valid lifecycle JSON."""
    payload = json.dumps(
        {
            "description": "A Python service.",
            "lifecycle": {
                "ci_system": "github-actions",
                "deployment_target": "pypi",
                "language_ecosystem": "python/poetry",
                "build_system": "poetry",
                "testing_framework": "pytest",
                "confidence": "high",
                "branching": {
                    "default_branch": "main",
                    "model": "trunk-based",
                    "release_branches": False,
                },
                "ci": {
                    "deploy_on": ["push"],
                    "trigger_events": ["push", "pull_request"],
                },
                "release": {
                    "versioning": "semver",
                    "artifact_types": ["pypi-package"],
                },
            },
        }
    )
    return InvocationResult(
        success=True,
        output=payload,
        error="",
        cli_used="codex",
        was_failover=False,
    )


# ---------------------------------------------------------------------------
# Tests — build_dep_map_dispatcher integration (public path)
# ---------------------------------------------------------------------------


def test_lifecycle_build_dispatcher_called_with_config(tmp_path: Path):
    """
    When LifecycleClaudeCliInvoker.__call__ is invoked, it calls
    build_dep_map_dispatcher exactly once, passing the config object
    obtained from get_config_service().get_config().
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-a"
    repo_path.mkdir()
    config = _make_mock_config()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_invocation_result()

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_build,
        _mock_config_svc(config),
    ):
        invoker = LifecycleClaudeCliInvoker()
        invoker("alias-a", repo_path)

    mock_build.assert_called_once()
    call_args = mock_build.call_args
    # Guard positional access before indexing to avoid IndexError when the
    # production call uses keyword-only arguments.
    config_passed = (call_args.args and call_args.args[0] is config) or (
        call_args.kwargs.get("config") is config
    )
    assert config_passed, (
        "build_dep_map_dispatcher must receive the config from get_config_service"
    )


# ---------------------------------------------------------------------------
# Tests — dispatcher construction state (private-method path, explicitly declared)
# ---------------------------------------------------------------------------


def test_lifecycle_build_dispatcher_claude_only_when_codex_disabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=False, _build_dispatcher() returns
    a Claude-only dispatcher (codex=None, effective weight=0.0).

    Exercises the private helper directly so we can inspect the built
    CliDispatcher state independent of the full __call__ path.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    config = _make_mock_config(codex_enabled=False)

    with _mock_config_svc(config):
        invoker = LifecycleClaudeCliInvoker()
        dispatcher = invoker._build_dispatcher()

    assert dispatcher.claude is not None
    assert dispatcher.codex is None, "codex must be None when codex disabled"
    assert dispatcher.codex_weight == 0.0


def test_lifecycle_build_dispatcher_codex_when_enabled(tmp_path: Path):
    """
    When codex_integration_config.enabled=True and CODEX_HOME is set,
    _build_dispatcher() returns a CliDispatcher with both claude and codex
    invokers present and codex_weight=1.0.

    Exercises the private helper directly so we can inspect the built
    CliDispatcher state independent of the full __call__ path.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    config = _make_mock_config(codex_enabled=True, codex_weight=1.0)
    codex_home = str(tmp_path / "codex-home")

    with (
        _mock_config_svc(config),
        patch.dict("os.environ", {"CODEX_HOME": codex_home}),
    ):
        invoker = LifecycleClaudeCliInvoker()
        dispatcher = invoker._build_dispatcher()

    assert dispatcher.claude is not None, "claude invoker must always be present"
    assert dispatcher.codex is not None, (
        "codex invoker must be set when codex enabled and CODEX_HOME is set"
    )
    assert dispatcher.codex_weight == 1.0


# ---------------------------------------------------------------------------
# Tests — dispatcher routing behaviour
# ---------------------------------------------------------------------------


def test_lifecycle_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    LifecycleClaudeCliInvoker.__call__ routes through dispatcher.dispatch
    with flow='repo_lifecycle' and does NOT call subprocess.run directly.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-x"
    repo_path.mkdir()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_invocation_result()

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        _mock_config_svc(_make_mock_config()),
        patch("subprocess.run") as mock_subproc,
    ):
        invoker = LifecycleClaudeCliInvoker()
        invoker("alias-x", repo_path)

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by LifecycleClaudeCliInvoker.__call__"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "repo_lifecycle", (
        f"flow must be 'repo_lifecycle', got {call_kwargs.get('flow')!r}"
    )
    mock_subproc.assert_not_called()


def test_lifecycle_dispatcher_failure_raises_runtime_error(tmp_path: Path):
    """
    When dispatcher.dispatch returns a failed InvocationResult,
    LifecycleClaudeCliInvoker.__call__ raises RuntimeError containing the
    alias name and the upstream error text.
    """
    from code_indexer.global_repos.lifecycle_claude_cli_invoker import (
        LifecycleClaudeCliInvoker,
    )

    repo_path = tmp_path / "alias-z"
    repo_path.mkdir()

    failed_result = InvocationResult(
        success=False,
        output="",
        error="Codex timed out after 420s",
        cli_used="codex",
        was_failover=False,
    )

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = failed_result

    with (
        patch(
            "code_indexer.global_repos.lifecycle_claude_cli_invoker.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ),
        _mock_config_svc(_make_mock_config()),
    ):
        invoker = LifecycleClaudeCliInvoker()

        with pytest.raises(RuntimeError) as exc_info:
            invoker("alias-z", repo_path)

    message = str(exc_info.value)
    assert "alias-z" in message, "RuntimeError must name the alias"
    assert "timed out" in message, "RuntimeError must preserve the upstream error"
