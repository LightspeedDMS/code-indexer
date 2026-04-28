"""
Tests for Bug #936: DependencyMapService wires dispatcher-backed callable into
DepMapRepairExecutor instead of raw invoke_claude_cli.

Strategy: build a real DependencyMapService with minimal concrete collaborators,
patch DepMapRepairExecutor at the class seam to capture invoke_llm_fn, call
run_graph_repair_dry_run, then invoke the captured callable (while the invoker
spy patch remains active) and assert routing through the correct CLI invoker.

Mocking policy:
  - config_manager, tracking_backend, analyzer, config-service: MagicMock
    (external infrastructure — not the system under test).
  - IntelligenceCliInvoker.invoke (ClaudeInvoker/CodexInvoker): spy functions
    (subprocess CLI boundary — the only real mock seam).
  - DependencyMapService and DepMapRepairExecutor: NOT mocked.
  - DepMapRepairExecutor class is replaced at its import point by a spy subclass
    that captures constructor kwargs and delegates to the real class (clean class-seam
    injection; no __init__ monkey-patching).

Test inventory (exhaustive list):
  test_service_passes_dispatcher_backed_callable_to_executor_codex_weight_one
  test_service_passes_claude_backed_callable_when_codex_disabled
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional
from unittest.mock import MagicMock, patch

# Timeout constants matching the bidirectional audit defaults (Bug #936 / Story #912).
_SHELL_TIMEOUT_SECONDS: int = 270
_OUTER_TIMEOUT_SECONDS: int = 330

# Config-service patch targets that must all receive the same fake config service.
_CONFIG_SVC_TARGETS = (
    "code_indexer.server.services.config_service.get_config_service",
    "code_indexer.server.web.dependency_map_routes.get_config_service",
    "code_indexer.server.services.dep_map_dispatcher_factory.get_config_service",
)


# ---------------------------------------------------------------------------
# Concrete minimal test doubles (not mocks of the SUT)
# ---------------------------------------------------------------------------


class _FakeTrackingBackend:
    """Concrete stub for DependencyMapTrackingBackend."""

    def get_state(self):
        return None

    def update_state(self, *a, **kw):
        pass

    def get_last_analysis_time(self):
        return None


class _FakeAnalyzer:
    """Concrete stub for DependencyMapAnalyzer."""

    pass


class _FakeGoldenReposManager:
    """Concrete stub for GoldenRepoManager."""

    def __init__(self, root: Path):
        self._root = root

    @property
    def golden_repos_dir(self) -> str:
        return str(self._root)

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._root / alias)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_codex_config(codex_enabled: bool, codex_weight: float):
    """Build a mock ServerConfig carrying codex integration settings."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    cfg.enable_graph_channel_repair = True
    cfg.graph_repair_self_loop = None
    cfg.graph_repair_malformed_yaml = None
    cfg.graph_repair_garbage_domain = None
    cfg.graph_repair_bidirectional_mismatch = None
    return cfg


def _make_real_service(root: Path):
    """Build a real DependencyMapService with minimal concrete collaborators."""
    from code_indexer.server.services.dependency_map_service import (
        DependencyMapService,
    )

    return DependencyMapService(
        golden_repos_manager=_FakeGoldenReposManager(root),
        config_manager=MagicMock(),
        tracking_backend=_FakeTrackingBackend(),
        analyzer=_FakeAnalyzer(),
    )


def _make_invocation_result(cli_used: str):
    from code_indexer.server.services.intelligence_cli_invoker import InvocationResult

    return InvocationResult(
        success=True,
        output=f"dispatched via {cli_used}",
        error="",
        cli_used=cli_used,
        was_failover=False,
    )


def _env_without_codex_home() -> dict:
    """Return os.environ copy with CODEX_HOME removed."""
    return {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}


def _drive_service_and_capture_invoke_fn(
    tmp_path: Path,
    config,
    env_overrides: dict,
) -> Optional[Callable]:
    """
    Build a real DependencyMapService, set up the dep_map directory, then call
    run_graph_repair_dry_run while a spy subclass of DepMapRepairExecutor is
    active at the service's import point.

    The spy subclass captures the invoke_llm_fn kwarg during __init__ and
    then delegates fully to DepMapRepairExecutor.__init__, preserving all
    real executor behavior.

    Returns the captured callable, or None if construction was not reached.
    """
    from code_indexer.server.services.dep_map_repair_executor import (
        DepMapRepairExecutor,
    )

    service = _make_real_service(tmp_path)
    (tmp_path / "cidx-meta" / "dependency-map").mkdir(parents=True)

    captured_fn: List[Optional[Callable]] = [None]

    class _SpyExecutor(DepMapRepairExecutor):
        """Capture invoke_llm_fn kwarg, then delegate to real DepMapRepairExecutor."""

        def __init__(self, *args, **kwargs):
            captured_fn[0] = kwargs.get("invoke_llm_fn")
            super().__init__(*args, **kwargs)

    mock_config_svc = MagicMock()
    mock_config_svc.get_config.return_value = config

    with (
        patch(_CONFIG_SVC_TARGETS[0], return_value=mock_config_svc, create=True),
        patch(_CONFIG_SVC_TARGETS[1], return_value=mock_config_svc, create=True),
        patch(_CONFIG_SVC_TARGETS[2], return_value=mock_config_svc, create=True),
        patch(
            "code_indexer.server.services.dep_map_repair_executor.DepMapRepairExecutor",
            _SpyExecutor,
        ),
        patch.dict("os.environ", env_overrides, clear=True),
    ):
        service.run_graph_repair_dry_run()

    return captured_fn[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_service_passes_dispatcher_backed_callable_to_executor_codex_weight_one(
    tmp_path: Path,
):
    """
    With codex_weight=1.0 and codex enabled + CODEX_HOME set, run_graph_repair_dry_run
    must wire invoke_llm_fn so that calling it routes through CodexInvoker.invoke.
    The invoker spy patch is active during the invoke_fn call to ensure routing evidence.
    """
    config = _make_codex_config(codex_enabled=True, codex_weight=1.0)
    env = {**os.environ, "CODEX_HOME": str(tmp_path / "codex-home")}

    codex_invoke_calls: List[dict] = []

    def _fake_codex_invoke(self_inner, flow, cwd, prompt, timeout):
        codex_invoke_calls.append({"flow": flow})
        return _make_invocation_result("codex")

    invoke_fn = _drive_service_and_capture_invoke_fn(tmp_path, config, env)
    assert invoke_fn is not None, "invoke_llm_fn must not be None"

    with patch(
        "code_indexer.server.services.codex_invoker.CodexInvoker.invoke",
        _fake_codex_invoke,
    ):
        ok, _ = invoke_fn(
            "/some/path", "prompt", _SHELL_TIMEOUT_SECONDS, _OUTER_TIMEOUT_SECONDS
        )

    assert ok is True
    assert len(codex_invoke_calls) >= 1, (
        f"CodexInvoker.invoke must be called when codex_weight=1.0; calls: {codex_invoke_calls}"
    )


def test_service_passes_claude_backed_callable_when_codex_disabled(tmp_path: Path):
    """
    With codex disabled, run_graph_repair_dry_run wires invoke_llm_fn so that
    calling it routes through ClaudeInvoker.invoke (preserving pre-#936 behavior).
    The invoker spy patch is active during the invoke_fn call.
    """
    config = _make_codex_config(codex_enabled=False, codex_weight=0.0)
    env = _env_without_codex_home()

    claude_invoke_calls: List[dict] = []

    def _fake_claude_invoke(self_inner, flow, cwd, prompt, timeout):
        claude_invoke_calls.append({"flow": flow})
        return _make_invocation_result("claude")

    invoke_fn = _drive_service_and_capture_invoke_fn(tmp_path, config, env)
    assert invoke_fn is not None, "invoke_llm_fn must not be None"

    with patch(
        "code_indexer.server.services.claude_invoker.ClaudeInvoker.invoke",
        _fake_claude_invoke,
    ):
        ok, _ = invoke_fn(
            "/some/path", "prompt", _SHELL_TIMEOUT_SECONDS, _OUTER_TIMEOUT_SECONDS
        )

    assert ok is True
    assert len(claude_invoke_calls) >= 1, (
        f"ClaudeInvoker.invoke must be called when codex disabled; calls: {claude_invoke_calls}"
    )
