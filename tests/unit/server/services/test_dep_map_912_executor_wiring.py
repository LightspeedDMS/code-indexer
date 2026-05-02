"""
Story #912 executor wiring tests.

Verifies that DepMapRepairExecutor:
  - accepts repo_path_resolver constructor parameter
  - routes BIDIRECTIONAL_MISMATCH anomalies through invoke_llm_fn (one call per anomaly)
  - skips bidirectional pass when enable_graph_channel_repair=False

Tests (exhaustive list):
  test_executor_accepts_repo_path_resolver_param
  test_bidirectional_mismatch_anomalies_trigger_audit
  test_bidirectional_pass_skipped_when_flag_false

Module-level helpers (exhaustive list):
  _FakeHealthDetector         -- minimal health detector returning healthy HealthReport
  _FakeIndexRegenerator       -- minimal index regenerator no-op
  _make_minimal_executor(tmp_path, invoke_fn, resolver, enable) -- build executor under test
  _make_bidi_anomaly(src, tgt) -- build BIDIRECTIONAL_MISMATCH AnomalyEntry for src->tgt
"""

from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor
from code_indexer.server.services.dep_map_health_detector import HealthReport
from code_indexer.server.services.dep_map_parser_hygiene import (
    AnomalyEntry,
    AnomalyType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHealthDetector:
    """Minimal health detector that always reports healthy."""

    def detect(self, output_dir):
        return HealthReport(
            status="healthy",
            is_healthy=True,
            anomalies=[],
            lifecycle=[],
        )


class _FakeIndexRegenerator:
    """Minimal index regenerator that is a no-op."""

    def regenerate(self, output_dir):
        pass


def _make_minimal_executor(
    tmp_path: Path,
    invoke_fn=None,
    resolver=None,
    enable: bool = True,
) -> DepMapRepairExecutor:
    """Build a DepMapRepairExecutor with minimal dependencies for wiring tests."""
    return DepMapRepairExecutor(
        health_detector=_FakeHealthDetector(),
        index_regenerator=_FakeIndexRegenerator(),
        enable_graph_channel_repair=enable,
        repo_path_resolver=resolver,
        invoke_llm_fn=invoke_fn,
    )


def _make_bidi_anomaly(src: str, tgt: str) -> AnomalyEntry:
    """Build a BIDIRECTIONAL_MISMATCH AnomalyEntry for src->tgt."""
    return AnomalyEntry(
        type=AnomalyType.BIDIRECTIONAL_MISMATCH,
        file=f"{tgt}.md",
        message=(
            f"bidirectional mismatch: {src}→{tgt} declared outgoing by {src}"
            " but not confirmed by incoming table"
        ),
        channel="data",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_executor_accepts_repo_path_resolver_param(tmp_path):
    """DepMapRepairExecutor constructor accepts repo_path_resolver kwarg without error."""
    executor = _make_minimal_executor(tmp_path, resolver=lambda alias: "")
    assert executor is not None


def test_bidirectional_mismatch_anomalies_trigger_audit(tmp_path):
    """One invoke_llm_fn call is made per BIDIRECTIONAL_MISMATCH anomaly in parser output."""
    call_count = [0]

    def counting_invoke(repo_path, prompt, shell_timeout, outer_timeout):
        call_count[0] += 1
        return True, (
            "VERDICT: REFUTED\n"
            "EVIDENCE_TYPE: none\n"
            "CITATIONS:\n"
            "REASONING: No evidence.\n"
        )

    executor = _make_minimal_executor(
        tmp_path, invoke_fn=counting_invoke, resolver=lambda a: ""
    )

    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()

    for domain in ("src", "tgt"):
        (dep_map_dir / f"{domain}.md").write_text(
            f"---\nname: {domain}\n---\n"
            f"# Domain Analysis: {domain}\n\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
    (dep_map_dir / "_domains.json").write_text(
        '[{"name":"src","description":"d","participating_repos":[],'
        '"last_analyzed":"2024-01-01T00:00:00"},'
        '{"name":"tgt","description":"d","participating_repos":[],'
        '"last_analyzed":"2024-01-01T00:00:00"}]'
    )

    anomaly = _make_bidi_anomaly("src", "tgt")

    with patch(
        "code_indexer.server.services.dep_map_repair_executor.DepMapMCPParser"
    ) as MockParser:
        mock_instance = MockParser.return_value
        mock_instance.get_cross_domain_graph_with_channels.return_value = (
            [],
            [anomaly],
            [],
            [anomaly],
        )
        fixed: List[str] = []
        errors: List[str] = []
        executor._run_phase37(dep_map_dir, fixed, errors)

    assert call_count[0] == 1


def test_build_repair_executor_wires_invoke_llm_fn(tmp_path):
    """_build_repair_executor wires _invoke_llm_fn through CliDispatcher.dispatch (C2).

    Proves dispatcher routing by:
    1. Patching CliDispatcher.dispatch with a MagicMock that returns a valid InvocationResult.
    2. Building the executor under the patch.
    3. Calling executor._invoke_llm_fn(...) directly inside the patch context.
    4. Using assert_called_once_with to verify dispatch() was called with the expected
       keyword arguments (flow=, cwd=, prompt=, timeout=).
    """
    from code_indexer.server.services.cli_dispatcher import CliDispatcher
    from code_indexer.server.services.intelligence_cli_invoker import InvocationResult
    from code_indexer.server.web.dependency_map_routes import _build_repair_executor

    fake_manager = MagicMock()
    fake_manager.get_actual_repo_path = lambda alias: str(tmp_path / alias)

    fake_dep_map_service = MagicMock()
    fake_dep_map_service._job_tracker = None

    fake_activity_journal = MagicMock()

    fake_config = MagicMock()
    fake_config.enable_graph_channel_repair = True
    fake_config.graph_repair_self_loop = None
    fake_config.graph_repair_malformed_yaml = None
    fake_config.graph_repair_garbage_domain = None
    fake_config.graph_repair_bidirectional_mismatch = None
    # codex disabled so CliDispatcher routes exclusively to claude
    fake_config.codex_integration_config = MagicMock()
    fake_config.codex_integration_config.enabled = False

    fake_config_service = MagicMock()
    fake_config_service.get_config.return_value = fake_config

    fake_result = InvocationResult(
        success=True,
        output="VERDICT: REFUTED\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: x\n",
        cli_used="claude",
        was_failover=False,
        error="",
    )

    test_repo = str(tmp_path / "test-repo")
    test_prompt = "test prompt text"

    mock_dispatch = MagicMock(return_value=fake_result)

    with (
        patch(
            "code_indexer.server.web.routes._get_golden_repo_manager",
            return_value=fake_manager,
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=fake_config_service,
        ),
        patch.object(CliDispatcher, "dispatch", mock_dispatch),
    ):
        executor = _build_repair_executor(
            fake_dep_map_service,
            tmp_path,
            fake_activity_journal,
        )

        assert executor._invoke_llm_fn is not None, (
            "_build_repair_executor must wire invoke_llm_fn (C2)"
        )
        assert executor._repo_path_resolver is not None, (
            "_build_repair_executor must wire repo_path_resolver (C2)"
        )

        # Actually invoke the captured callable — this is the proof of routing.
        ok, out = executor._invoke_llm_fn(test_repo, test_prompt, 60, 120)

    assert ok is True, "_invoke_llm_fn must return success=True from fake dispatch"
    # Verify dispatch() was called with the exact keyword args the call site uses.
    mock_dispatch.assert_called_once_with(
        flow="dep_map_repair",
        cwd=test_repo,
        prompt=test_prompt,
        timeout=120,
    )


def test_bidirectional_pass_skipped_when_flag_false(tmp_path):
    """When enable_graph_channel_repair=False, invoke_llm_fn is never called."""
    call_count = [0]

    def counting_invoke(repo_path, prompt, shell_timeout, outer_timeout):
        call_count[0] += 1
        return True, "VERDICT: REFUTED\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: x\n"

    executor = _make_minimal_executor(
        tmp_path, invoke_fn=counting_invoke, resolver=lambda a: "", enable=False
    )
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()

    anomaly = _make_bidi_anomaly("src", "tgt")

    with patch(
        "code_indexer.server.services.dep_map_repair_executor.DepMapMCPParser"
    ) as MockParser:
        mock_instance = MockParser.return_value
        mock_instance.get_cross_domain_graph_with_channels.return_value = (
            [],
            [anomaly],
            [],
            [anomaly],
        )
        fixed: List[str] = []
        errors: List[str] = []
        executor._run_phase37(dep_map_dir, fixed, errors)

    assert call_count[0] == 0


# ---------------------------------------------------------------------------
# AC10 / AC12 new tests
# ---------------------------------------------------------------------------


def test_no_bare_except_exception_in_bidi_modules():
    """AC10: no bare `except Exception` clause in any of the four bidirectional modules."""
    import pathlib

    services_dir = (
        pathlib.Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "services"
    )
    modules = [
        "dep_map_repair_bidirectional.py",
        "dep_map_repair_bidirectional_backfill.py",
        "dep_map_repair_bidirectional_parser.py",
        "dep_map_repair_bidirectional_verify.py",
    ]
    violations: List[str] = []
    for module_name in modules:
        path = services_dir / module_name
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("except Exception"):
                violations.append(f"{module_name}:{lineno}: {stripped!r}")
    assert violations == [], (
        "Bare `except Exception` found in bidirectional modules:\n"
        + "\n".join(violations)
    )


def test_audit_bidirectional_mismatch_shim_exists():
    """AC12: DepMapRepairExecutor must expose a _audit_bidirectional_mismatch method."""
    assert hasattr(DepMapRepairExecutor, "_audit_bidirectional_mismatch"), (
        "DepMapRepairExecutor is missing the _audit_bidirectional_mismatch shim (AC12)"
    )
    assert callable(getattr(DepMapRepairExecutor, "_audit_bidirectional_mismatch")), (
        "_audit_bidirectional_mismatch must be callable"
    )


class _SpyExecutor(DepMapRepairExecutor):
    """Test spy subclass that records calls to _audit_bidirectional_mismatch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shim_calls: List = []

    def _audit_bidirectional_mismatch(
        self,
        output_dir,
        anomaly,
        domains_json,
        fixed,
        errors,
        dry_run=False,
        would_be_writes=None,
        **kwargs,
    ):
        self.shim_calls.append(anomaly)


def test_run_phase37_routes_through_shim(tmp_path):
    """AC12: _run_phase37 routes BIDIRECTIONAL_MISMATCH anomalies through the shim, not directly."""
    spy = _SpyExecutor(
        health_detector=_FakeHealthDetector(),
        index_regenerator=_FakeIndexRegenerator(),
        enable_graph_channel_repair=True,
        repo_path_resolver=lambda a: "",
        invoke_llm_fn=lambda *a: (
            True,
            "VERDICT: REFUTED\nEVIDENCE_TYPE: none\nCITATIONS:\nREASONING: x\n",
        ),
    )

    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text(
        '[{"name":"src","description":"d","participating_repos":[],'
        '"last_analyzed":"2024-01-01T00:00:00"},'
        '{"name":"tgt","description":"d","participating_repos":[],'
        '"last_analyzed":"2024-01-01T00:00:00"}]'
    )

    anomaly = _make_bidi_anomaly("src", "tgt")

    with patch(
        "code_indexer.server.services.dep_map_repair_executor.DepMapMCPParser"
    ) as MockParser:
        mock_instance = MockParser.return_value
        mock_instance.get_cross_domain_graph_with_channels.return_value = (
            [],
            [anomaly],
            [],
            [anomaly],
        )
        fixed: List[str] = []
        errors: List[str] = []
        spy._run_phase37(dep_map_dir, fixed, errors)

    assert len(spy.shim_calls) == 1, (
        f"_audit_bidirectional_mismatch shim must be called once per anomaly, "
        f"got {len(spy.shim_calls)}"
    )
    assert spy.shim_calls[0] is anomaly, (
        "The anomaly passed to the shim must be the BIDIRECTIONAL_MISMATCH AnomalyEntry"
    )
