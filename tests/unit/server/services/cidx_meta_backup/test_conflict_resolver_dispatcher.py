"""
Dispatcher-routing tests for ClaudeConflictResolver (Bug #936).

Verifies that ClaudeConflictResolver.resolve routes its LLM call through
CliDispatcher using flow="cidx_meta_conflict" instead of calling
invoke_claude_cli directly, and that it builds the dispatcher via
build_dep_map_dispatcher from the config.

Anti-mock rule: only external boundaries are patched (get_config_service,
build_dep_map_dispatcher, CodexInvoker.invoke, ClaudeInvoker.invoke,
subprocess.run). ClaudeConflictResolver and CliDispatcher are never mocked
or partially stubbed.

Test inventory:
  test_conflict_resolver_build_dispatcher_called_with_config
  test_conflict_resolver_dispatches_with_correct_flow_name
  test_conflict_resolver_failed_result_returns_failure
  test_conflict_resolver_dispatch_exception_propagates
  test_conflict_resolver_failover_codex_to_claude_succeeds
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.intelligence_cli_invoker import (
    FailureClass,
    InvocationResult,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MODULE = "code_indexer.server.services.cidx_meta_backup.conflict_resolver"


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
    cfg.cidx_meta_backup_config = None
    return cfg


def _make_success_result(cli_used: str = "claude") -> InvocationResult:
    return InvocationResult(
        success=True,
        output="resolved all conflicts",
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


def _git_no_unmerged() -> CompletedProcess:
    return CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")


@contextmanager
def _resolver_context(
    mock_dispatcher: MagicMock,
    config: Optional[MagicMock] = None,
    git_result: Optional[CompletedProcess] = None,
):
    """Patch external boundaries for resolver tests using a mock dispatcher."""
    if config is None:
        config = _make_mock_config()
    if git_result is None:
        git_result = _git_no_unmerged()

    mock_svc = MagicMock()
    mock_svc.get_config.return_value = config

    with (
        patch(
            f"{_MODULE}.build_dep_map_dispatcher",
            return_value=mock_dispatcher,
        ) as mock_build,
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
        patch(f"{_MODULE}.subprocess.run", return_value=git_result),
    ):
        yield mock_build, config


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_conflict_resolver_build_dispatcher_called_with_config(tmp_path: Path):
    """
    When ClaudeConflictResolver.resolve is called, it calls
    build_dep_map_dispatcher exactly once, passing the config object
    obtained from get_config_service().get_config().
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    config = _make_mock_config()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_result()

    with _resolver_context(mock_dispatcher, config=config) as (mock_build, cfg):
        result = ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")

    assert result.success is True
    mock_build.assert_called_once()
    call_args = mock_build.call_args
    config_passed = (call_args.args and call_args.args[0] is cfg) or (
        call_args.kwargs.get("config") is cfg
    )
    assert config_passed, (
        "build_dep_map_dispatcher must receive the config from get_config_service"
    )


def test_conflict_resolver_dispatches_with_correct_flow_name(tmp_path: Path):
    """
    ClaudeConflictResolver.resolve routes through dispatcher.dispatch
    with flow='cidx_meta_conflict'.
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_success_result()

    with _resolver_context(mock_dispatcher):
        ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")

    assert mock_dispatcher.dispatch.called, (
        "dispatcher.dispatch must be called by ClaudeConflictResolver.resolve"
    )
    call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
    assert call_kwargs.get("flow") == "cidx_meta_conflict", (
        f"flow must be 'cidx_meta_conflict', got {call_kwargs.get('flow')!r}"
    )


def test_conflict_resolver_failed_result_returns_failure(tmp_path: Path):
    """
    When dispatcher.dispatch returns a failed InvocationResult,
    ClaudeConflictResolver.resolve returns ResolverResult(success=False)
    with the upstream error preserved.
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.return_value = _make_failed_result(
        error="Codex timed out after 600s"
    )

    with _resolver_context(mock_dispatcher):
        result = ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")

    assert result.success is False
    assert result.error is not None
    assert "timed out" in result.error


def test_conflict_resolver_dispatch_exception_propagates(tmp_path: Path):
    """
    When dispatcher.dispatch raises RuntimeError (e.g. unexpected CLI crash),
    the exception propagates out of ClaudeConflictResolver.resolve.
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )

    repo = tmp_path / "cidx-meta"
    repo.mkdir()

    mock_dispatcher = MagicMock()
    mock_dispatcher.dispatch.side_effect = RuntimeError("unexpected dispatcher crash")

    with _resolver_context(mock_dispatcher):
        with pytest.raises(RuntimeError, match="unexpected dispatcher crash"):
            ClaudeConflictResolver().resolve(str(repo), ["docs/a.md"], "master")


def test_conflict_resolver_failover_codex_to_claude_succeeds(tmp_path: Path):
    """
    Failover coverage: when Codex primary invoke returns RETRYABLE_ON_OTHER,
    the real CliDispatcher automatically falls back to Claude and the resolver
    returns success.

    This test uses the REAL CliDispatcher (build_dep_map_dispatcher is NOT mocked)
    and patches the concrete seam:
      - CodexInvoker.invoke: returns RETRYABLE_ON_OTHER failure (primary fails).
      - ClaudeInvoker.invoke: returns success (fallback succeeds).

    The real CliDispatcher.dispatch drives the failover so the test exercises
    the actual failover logic, not a fabricated pre-computed result.
    """
    from code_indexer.server.services.cidx_meta_backup.conflict_resolver import (
        ClaudeConflictResolver,
    )
    from code_indexer.server.services.claude_invoker import ClaudeInvoker
    from code_indexer.server.services.codex_invoker import CodexInvoker

    repo = tmp_path / "cidx-meta"
    repo.mkdir()
    codex_home = str(tmp_path / "codex-home")

    # Codex-enabled config with weight=1.0 so Codex is always selected as primary.
    codex_config = _make_mock_config(codex_enabled=True, codex_weight=1.0)
    mock_svc = MagicMock()
    mock_svc.get_config.return_value = codex_config

    codex_invoke_calls: list = []
    claude_invoke_calls: list = []

    def _codex_invoke(self, flow, cwd, prompt, timeout):
        codex_invoke_calls.append((flow, cwd))
        return _make_failed_result(
            error="Codex process failed",
            cli_used="codex",
            failure_class=FailureClass.RETRYABLE_ON_OTHER,
        )

    def _claude_invoke(self, flow, cwd, prompt, timeout):
        claude_invoke_calls.append((flow, cwd))
        return _make_success_result(cli_used="claude")

    with (
        patch(f"{_MODULE}.get_config_service", return_value=mock_svc),
        patch(f"{_MODULE}.subprocess.run", return_value=_git_no_unmerged()),
        patch.dict("os.environ", {"CODEX_HOME": codex_home}),
        patch.object(CodexInvoker, "invoke", _codex_invoke),
        patch.object(ClaudeInvoker, "invoke", _claude_invoke),
    ):
        result = ClaudeConflictResolver().resolve(
            str(repo), ["docs/conflict.md"], "master"
        )

    assert result.success is True, (
        "resolver must succeed when the real dispatcher falls over from Codex to Claude"
    )
    assert len(codex_invoke_calls) == 1, (
        "Codex primary must be invoked exactly once before failover"
    )
    assert len(claude_invoke_calls) == 1, (
        "Claude fallback must be invoked exactly once after Codex fails with RETRYABLE_ON_OTHER"
    )
