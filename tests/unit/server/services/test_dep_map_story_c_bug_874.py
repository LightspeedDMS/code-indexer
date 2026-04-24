"""
Unit tests for Bug #874 Story C: wire run_type + phase_timings_json + repos_skipped
into all three call paths (full, delta, refinement).

Story B plumbed the kwargs through _record_run_metrics — Story C wires the callers.

Tests:
  1. test_full_run_records_run_type_full_and_phase_timings_json
  2. test_delta_run_records_run_type_delta_and_phase_timings_json
  3. test_refinement_run_records_run_type_refinement_and_phase_timings_json
  4. test_refinement_run_domain_count_is_batch_size_not_total_list
  5. test_delta_repos_skipped_is_honest_non_negative_int
  6. test_full_run_phase_timings_finalize_s_present_and_non_negative

Mock strategy (Messi Rule 01 — anti-mock):
  - Only injected collaborators mocked: _tracking_backend, _golden_repos_manager,
    _analyzer (external Claude CLI), _config_manager.
  - No patching of SUT internal methods.
  - Filesystem built so the SUT can fully execute (staging dir created/renamed by SUT).
  - _analyzer methods (build_refinement_prompt, invoke_refinement_file, etc.)
    are the external Claude CLI boundary — legitimate mock targets via the injected mock.

repos_skipped (FR6): Story C instructs that delta repos_skipped should be derived
  honestly or deferred with a TODO comment if not derivable without a refactor.
  Test 5 asserts the value is a non-negative int (not None), which is the honest
  contract for both "derived" and "deferred-to-zero" outcomes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_INDEX_MD_WITH_MAPPING = """\
# Dependency Map Index

## Repo-to-Domain Matrix

| Repository | Domains |
|------------|---------|
| repo1 | auth |

## Cross-Domain Dependencies

| Source | Target | Type |
|--------|--------|------|
"""

# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _build_full_analysis_fs(tmp_path: Path) -> None:
    """Create minimal filesystem for run_full_analysis.

    SUT creates dependency-map.staging/, renames it to dependency-map/.
    We only need the repo clone to exist.
    """
    (tmp_path / "repo1").mkdir(parents=True)
    (tmp_path / "repo1" / "main.py").write_text("print('hello')")


def _build_delta_fs(tmp_path: Path) -> Path:
    """Create minimal filesystem for run_delta_analysis (repo1 as new — no stored hash).

    Returns the repo1 clone path.
    """
    repo1 = tmp_path / "repo1"
    repo1.mkdir(parents=True)
    (repo1 / "main.py").write_text("print('hello')")

    live_depmap = tmp_path / "cidx-meta" / "dependency-map"
    live_depmap.mkdir(parents=True)
    (live_depmap / "auth.md").write_text("auth domain content")

    versioned_depmap = (
        tmp_path / ".versioned" / "cidx-meta" / "v_001" / "dependency-map"
    )
    versioned_depmap.mkdir(parents=True)
    (versioned_depmap / "_index.md").write_text(_INDEX_MD_WITH_MAPPING)
    (versioned_depmap / "auth.md").write_text("existing auth content")
    return repo1


def _build_refinement_fs(tmp_path: Path, domain_count: int) -> None:
    """Create filesystem for run_refinement_cycle with `domain_count` domains.

    Domain files exist in the versioned (read) path so refine_or_create_domain
    takes the _refine_existing_domain branch — calling _analyzer.build_refinement_prompt
    and invoke_refinement_file (external Claude CLI, mocked via injected _analyzer).
    """
    versioned_depmap = (
        tmp_path / ".versioned" / "cidx-meta" / "v_001" / "dependency-map"
    )
    versioned_depmap.mkdir(parents=True)
    domains = [{"name": f"domain_{i}"} for i in range(domain_count)]
    (versioned_depmap / "_domains.json").write_text(json.dumps(domains))
    for d in domains:
        (versioned_depmap / f"{d['name']}.md").write_text(f"content for {d['name']}")
    (tmp_path / "cidx-meta" / "dependency-map").mkdir(parents=True)


# ---------------------------------------------------------------------------
# Shared mock factories
# ---------------------------------------------------------------------------


def _tracking_mock(
    stored_hashes: Optional[Dict[str, Any]] = None,
    refinement_cursor: int = 0,
) -> Mock:
    t = Mock()
    t.get_tracking.return_value = {
        "status": "completed",
        "commit_hashes": json.dumps(stored_hashes or {}),
        "refinement_cursor": refinement_cursor,
    }
    return t


def _gm_mock(tmp_path: Path, repos: List[Dict[str, Any]]) -> Mock:
    gm = Mock()
    gm.golden_repos_dir = str(tmp_path)
    gm.list_golden_repos.return_value = repos
    gm.get_actual_repo_path.side_effect = lambda alias: next(
        (str(r["clone_path"]) for r in repos if r.get("alias") == alias), None
    )
    return gm


def _make_svc(
    tmp_path: Path,
    repos: List[Dict[str, Any]],
    tracking: Mock,
    analyzer: Mock,
    config: Mock,
) -> DependencyMapService:
    config_mgr = Mock()
    config_mgr.get_claude_integration_config.return_value = config
    gm = _gm_mock(tmp_path, repos)
    svc = DependencyMapService(gm, config_mgr, tracking, analyzer)
    svc._job_tracker = None
    svc._lifecycle_invoker = None
    svc._lifecycle_debouncer = None
    svc._refresh_scheduler = None
    return svc


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_phase_timings_keys(call_kwargs: Dict, *keys: str) -> None:
    """Assert phase_timings_json is present, parseable, and contains all keys >= 0."""
    ptj = call_kwargs.get("phase_timings_json")
    assert ptj is not None, "phase_timings_json must not be None"
    timings = json.loads(ptj)
    for key in keys:
        assert key in timings, f"{key} missing from phase_timings_json: {timings}"
        assert isinstance(timings[key], float) and timings[key] >= 0.0, (
            f"{key}={timings[key]!r} must be a non-negative float"
        )


# ---------------------------------------------------------------------------
# Module-level fixtures: full-run service
# ---------------------------------------------------------------------------


@pytest.fixture()
def full_svc_and_tracking(tmp_path):
    """Service wired for run_full_analysis with mocked Pass1/Pass2 analyzer."""
    _build_full_analysis_fs(tmp_path)
    tracking = _tracking_mock()
    repos = [{"alias": "repo1", "clone_path": str(tmp_path / "repo1")}]
    analyzer = Mock()
    analyzer.run_pass_1_synthesis.return_value = [{"name": "auth"}]
    analyzer.run_pass_2_per_domain.return_value = None
    analyzer._reconcile_domains_json.side_effect = lambda _s, dl: dl
    analyzer._generate_index_md.return_value = None
    analyzer.generate_claude_md.return_value = None
    cfg = Mock()
    cfg.dependency_map_enabled = True
    cfg.dependency_map_interval_hours = 24
    cfg.dep_map_fact_check_enabled = False
    cfg.dependency_map_pass_timeout_seconds = 300
    cfg.dependency_map_pass2_max_turns = 5
    svc = _make_svc(tmp_path, repos, tracking, analyzer, cfg)
    return svc, tracking


# ---------------------------------------------------------------------------
# Module-level fixtures: delta-run service
# ---------------------------------------------------------------------------


@pytest.fixture()
def delta_svc_and_tracking(tmp_path):
    """Service wired for run_delta_analysis with repo1 as new (empty stored hashes)."""
    repo1 = _build_delta_fs(tmp_path)
    tracking = _tracking_mock(stored_hashes={})
    repos = [{"alias": "repo1", "clone_path": str(repo1)}]
    analyzer = Mock()
    analyzer.build_delta_merge_prompt.return_value = "prompt"
    analyzer.invoke_delta_merge_file.return_value = "updated auth content"
    analyzer.generate_claude_md.return_value = None
    cfg = Mock()
    cfg.dependency_map_enabled = True
    cfg.dependency_map_interval_hours = 24
    cfg.dep_map_fact_check_enabled = False
    cfg.dependency_map_pass_timeout_seconds = 300
    cfg.dependency_map_delta_max_turns = 5
    svc = _make_svc(tmp_path, repos, tracking, analyzer, cfg)
    return svc, tracking


# ---------------------------------------------------------------------------
# Module-level fixtures: refinement service (parameterised by domain_count, batch_size)
# ---------------------------------------------------------------------------


def _refinement_fixture(tmp_path: Path, domain_count: int, batch_size: int):
    """Factory used by refinement fixtures to avoid duplication."""
    _build_refinement_fs(tmp_path, domain_count=domain_count)
    tracking = _tracking_mock(refinement_cursor=0)
    analyzer = Mock()
    analyzer.build_refinement_prompt.return_value = "prompt"
    analyzer.invoke_refinement_file.return_value = "refined content"
    analyzer._generate_index_md.return_value = None
    cfg = Mock()
    cfg.dependency_map_enabled = True
    cfg.refinement_enabled = True
    cfg.refinement_domains_per_run = batch_size
    cfg.dependency_map_interval_hours = 24
    cfg.dependency_map_pass_timeout_seconds = 300
    cfg.dependency_map_delta_max_turns = 5
    svc = _make_svc(tmp_path, [], tracking, analyzer, cfg)
    return svc, tracking


@pytest.fixture()
def refinement_svc_and_tracking(tmp_path):
    """Refinement service with 5 domains, batch_size=3."""
    return _refinement_fixture(tmp_path, domain_count=5, batch_size=3)


@pytest.fixture()
def refinement_large_svc_and_tracking(tmp_path):
    """Refinement service with 20 domains, batch_size=5 — for batch-size test."""
    return _refinement_fixture(tmp_path, domain_count=20, batch_size=5)


# ---------------------------------------------------------------------------
# Test 1: Full run -> run_type="full" + phase_timings_json {synth_s, per_domain_s, finalize_s}
# ---------------------------------------------------------------------------


class TestFullRunRunTypeAndPhaseTimings:
    """Story C FR4: run_full_analysis must emit run_type='full' + all phase keys."""

    def test_full_run_records_run_type_full_and_phase_timings_json(
        self, full_svc_and_tracking
    ):
        svc, tracking = full_svc_and_tracking
        svc.run_full_analysis()

        assert tracking.record_run_metrics.called, (
            "Story C: record_run_metrics must be called by run_full_analysis"
        )
        call_kwargs = tracking.record_run_metrics.call_args.kwargs
        assert call_kwargs.get("run_type") == "full", (
            f"Expected run_type='full', got {call_kwargs.get('run_type')!r}"
        )
        _assert_phase_timings_keys(call_kwargs, "synth_s", "per_domain_s", "finalize_s")


# ---------------------------------------------------------------------------
# Test 2: Delta run -> run_type="delta" + phase_timings_json {detect_s, merge_s, finalize_s}
# ---------------------------------------------------------------------------


class TestDeltaRunRunTypeAndPhaseTimings:
    """Story C FR3: run_delta_analysis must emit run_type='delta' + all phase keys."""

    def test_delta_run_records_run_type_delta_and_phase_timings_json(
        self, delta_svc_and_tracking
    ):
        svc, tracking = delta_svc_and_tracking
        svc.run_delta_analysis()

        assert tracking.record_run_metrics.called, (
            "Story C: record_run_metrics must be called by delta path"
        )
        call_kwargs = tracking.record_run_metrics.call_args.kwargs
        assert call_kwargs.get("run_type") == "delta", (
            f"Expected run_type='delta', got {call_kwargs.get('run_type')!r}"
        )
        _assert_phase_timings_keys(call_kwargs, "detect_s", "merge_s", "finalize_s")


# ---------------------------------------------------------------------------
# Test 3: Refinement -> first-ever _record_run_metrics call with run_type="refinement"
# ---------------------------------------------------------------------------


class TestRefinementRunCallsRecordRunMetrics:
    """Story C FR5: run_refinement_cycle must call _record_run_metrics (never did before)."""

    def test_refinement_run_records_run_type_refinement_and_phase_timings_json(
        self, refinement_svc_and_tracking
    ):
        svc, tracking = refinement_svc_and_tracking
        svc.run_refinement_cycle()

        assert tracking.record_run_metrics.called, (
            "Story C FR5: record_run_metrics must be called by run_refinement_cycle — "
            "this call path was entirely absent before Story C"
        )
        call_kwargs = tracking.record_run_metrics.call_args.kwargs
        assert call_kwargs.get("run_type") == "refinement", (
            f"Expected run_type='refinement', got {call_kwargs.get('run_type')!r}"
        )
        _assert_phase_timings_keys(call_kwargs, "refine_s")


# ---------------------------------------------------------------------------
# Test 4: Refinement domain_count == batch_size (O1 from bug body)
# ---------------------------------------------------------------------------


class TestRefinementDomainCountIsBatchSize:
    """Story C O1: domain_count in refinement metrics must equal batch_size, not total."""

    def test_refinement_run_domain_count_is_batch_size_not_total_list(
        self, refinement_large_svc_and_tracking
    ):
        """20 domains total, batch_size=5 -> domain_count == 5."""
        svc, tracking = refinement_large_svc_and_tracking
        svc.run_refinement_cycle()

        assert tracking.record_run_metrics.called
        metrics = tracking.record_run_metrics.call_args[0][0]
        assert metrics["domain_count"] == 5, (
            f"domain_count must be batch_size=5 (what this cycle touched), "
            f"not total domain list length=20. Got {metrics['domain_count']}"
        )


# ---------------------------------------------------------------------------
# Test 5: Delta repos_skipped is a non-negative int (FR6)
# ---------------------------------------------------------------------------


class TestDeltaReposSkippedHonestInt:
    """Story C FR6: repos_skipped for delta must be a non-negative int, not None."""

    def test_delta_repos_skipped_is_honest_non_negative_int(
        self, delta_svc_and_tracking
    ):
        """
        repos_skipped must be an int >= 0.

        With 1 activated repo whose single domain is affected, the derivable value
        is 0 (all repos touched). Story C may defer the exact computation with a
        TODO #874 comment; either way the value must be a non-None, non-negative int.
        """
        svc, tracking = delta_svc_and_tracking
        svc.run_delta_analysis()

        assert tracking.record_run_metrics.called
        metrics = tracking.record_run_metrics.call_args[0][0]
        repos_skipped = metrics.get("repos_skipped")
        assert repos_skipped is not None, "repos_skipped must not be None for delta run"
        assert isinstance(repos_skipped, int), (
            f"repos_skipped must be int, got {type(repos_skipped).__name__}: {repos_skipped!r}"
        )
        assert repos_skipped >= 0, f"repos_skipped must be >= 0, got {repos_skipped}"


# ---------------------------------------------------------------------------
# Test 6: Full run finalize_s present and non-negative in phase_timings_json
# ---------------------------------------------------------------------------


class TestFullRunFinalizeTimingPresent:
    """Story C FR4: finalize_s key must appear in full-run phase_timings_json."""

    def test_full_run_phase_timings_finalize_s_present_and_non_negative(
        self, full_svc_and_tracking
    ):
        """
        phase_timings_json for full run must contain finalize_s >= 0.0.
        Confirms time.time() wrap around _finalize_analysis is wired (Story C FR4).
        """
        svc, tracking = full_svc_and_tracking
        svc.run_full_analysis()

        assert tracking.record_run_metrics.called
        call_kwargs = tracking.record_run_metrics.call_args.kwargs
        ptj = call_kwargs.get("phase_timings_json")
        assert ptj is not None, "phase_timings_json must not be None for full run"
        timings = json.loads(ptj)
        assert "finalize_s" in timings, (
            f"finalize_s missing — time.time() wrap around _finalize_analysis not added: {timings}"
        )
        assert isinstance(timings["finalize_s"], float), (
            f"finalize_s must be float, got {type(timings['finalize_s']).__name__}"
        )
        assert timings["finalize_s"] >= 0.0, (
            f"finalize_s must be >= 0.0, got {timings['finalize_s']}"
        )
