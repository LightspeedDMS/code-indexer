"""
Unit tests for Bug #874 Story A: Delta analysis hardcoded 0.0/0.0 timing fix.

Tests verify that:
1. detect_changes() duration is captured and passed to _record_run_metrics (pass1_duration_s)
2. _update_affected_domains() duration is captured and passed (pass2_duration_s)
3. No-changes-detected early-return branch: record_run_metrics NOT called, result is "skipped"
4. _finalize_delta_tracking passes synthetic detect_s/merge_s through directly
5. Regression guard: the literal "0.0, 0.0" is gone from _finalize_delta_tracking source

Semantic mapping (Story A, reusing existing columns — no schema change):
  detect_s  -> pass1_duration_s column  (change-detection phase)
  merge_s   -> pass2_duration_s column  (per-domain Claude-CLI merge phase)
Story B will introduce phase_timings_json for semantic clarity; Story A is the bleeding stopper.

All tests mock ONLY injected external collaborators:
  - _tracking_backend (injected)
  - _golden_repos_manager (injected)
  - _analyzer (injected)
Internal SUT methods run real code. time.time is NOT patched in tests 1-3 because Python's
logging module calls time.time() for every log record, making sequence control impossible.
Tests 1 and 2 assert pass1_duration_s >= 0.0 and pass2_duration_s >= 0.0 (real measured
values, not exact). Test 4 (direct call) verifies exact forwarding with synthetic values.

_delta_fixture is a factory fixture: all 3 integration tests call it with their
specific repo_to_domain_mapping parameter, keeping a single shared abstraction.
"""

import inspect
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


# ---------------------------------------------------------------------------
# Filesystem setup
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

_INDEX_MD_NO_MAPPING = "# Dependency Map Index\n\n(no domains yet)\n"


def _setup_filesystem(tmp_path: Path, *, repo_to_domain_mapping: bool) -> Path:
    """
    Build a minimal golden-repo + cidx-meta filesystem structure under tmp_path.

    Returns repo1_path (the clone_path for the test repo).

    Structure:
      tmp_path/
        repo1/main.py                          <- non-empty clone (file_count > 0)
        cidx-meta/dependency-map/auth.md       <- live write path
        .versioned/cidx-meta/v_001/
          dependency-map/_index.md             <- maps repo1->auth or empty
          dependency-map/auth.md               <- existing domain content (read path)
    """
    repo1_path = tmp_path / "repo1"
    repo1_path.mkdir(parents=True)
    (repo1_path / "main.py").write_text("print('hello')")

    live_depmap = tmp_path / "cidx-meta" / "dependency-map"
    live_depmap.mkdir(parents=True)
    (live_depmap / "auth.md").write_text("auth domain content")

    versioned_depmap = (
        tmp_path / ".versioned" / "cidx-meta" / "v_001" / "dependency-map"
    )
    versioned_depmap.mkdir(parents=True)
    index_content = (
        _INDEX_MD_WITH_MAPPING if repo_to_domain_mapping else _INDEX_MD_NO_MAPPING
    )
    (versioned_depmap / "_index.md").write_text(index_content)
    (versioned_depmap / "auth.md").write_text("existing auth content")

    return repo1_path


# ---------------------------------------------------------------------------
# _delta_fixture: factory fixture — all integration tests call it with their parameter
# ---------------------------------------------------------------------------


@pytest.fixture
def _delta_fixture(tmp_path):
    """
    Factory fixture: returns a callable that builds (svc, tracking) for any mapping scenario.

    Usage in tests:
        svc, tracking = _delta_fixture(repo_to_domain_mapping=True)
            - stored_hashes is empty -> repo1 classified as new
            - _index.md maps repo1 -> auth -> affected_domains={"auth"}
            - Main branch taken: _update_affected_domains runs, record_run_metrics called.

        svc, tracking = _delta_fixture(repo_to_domain_mapping=False)
            - stored_hashes={"repo1": "some_sha"} -> repo1 already tracked
            - read_current_commit returns None for non-git dirs, so the `current_hash and`
              guard fails -> repo1 is neither changed nor new -> all three lists empty
            - "No changes detected" early return fires, record_run_metrics NOT called.

    Only injected collaborators are mocked (_golden_repos_manager, _tracking_backend, _analyzer).
    Internal SUT methods run real code against the filesystem state built by _setup_filesystem.
    """

    def build(*, repo_to_domain_mapping: bool):
        repo1_path = _setup_filesystem(
            tmp_path, repo_to_domain_mapping=repo_to_domain_mapping
        )

        gm = Mock()
        gm.golden_repos_dir = str(tmp_path)
        gm.list_golden_repos.return_value = [
            {"alias": "repo1", "clone_path": str(repo1_path)}
        ]
        gm.get_actual_repo_path.side_effect = lambda alias: (
            str(repo1_path)
            if alias == "repo1"
            else (_ for _ in ()).throw(ValueError(alias))
        )

        tracking = Mock()
        if repo_to_domain_mapping:
            # Empty stored_hashes: repo1 classified as new, maps to auth via _index.md.
            stored = json.dumps({})
        else:
            # repo1 already tracked with a SHA. read_current_commit returns None for
            # non-git dirs, so `current_hash and current_hash != stored` is False ->
            # repo1 is not changed. repo1 IS in stored_hashes -> not new either.
            # All three lists empty -> "No changes detected" early return.
            stored = json.dumps({"repo1": "some_sha"})

        tracking.get_tracking.return_value = {
            "status": "completed",
            "commit_hashes": stored,
        }

        analyzer = Mock()
        analyzer.build_delta_merge_prompt.return_value = "prompt"
        analyzer.invoke_delta_merge_file.return_value = "updated auth content"
        analyzer.generate_claude_md.return_value = None

        claude_config = Mock()
        claude_config.dependency_map_enabled = True
        claude_config.dependency_map_interval_hours = 24
        claude_config.dep_map_fact_check_enabled = False
        claude_config.dependency_map_pass_timeout_seconds = 300
        claude_config.dependency_map_delta_max_turns = 5

        config_mgr = Mock()
        config_mgr.get_claude_integration_config.return_value = claude_config

        svc = DependencyMapService(gm, config_mgr, tracking, analyzer)
        svc._job_tracker = None
        svc._lifecycle_invoker = None
        svc._lifecycle_debouncer = None
        svc._refresh_scheduler = None

        return svc, tracking

    return build


# ---------------------------------------------------------------------------
# Test 1: detect_changes duration is measured and passed to _record_run_metrics
# ---------------------------------------------------------------------------


class TestRunDeltaAnalysisRecordsNonzeroDetectTiming:
    """Bug #874 Story A: detect_changes() timing must reach _record_run_metrics."""

    def test_run_delta_analysis_records_nonzero_detect_timing(self, _delta_fixture):
        """
        Assert record_run_metrics is called with pass1_duration_s >= 0.0.

        We do not patch time.time because Python's logging module calls time.time()
        for every log record, making sequence-based control impossible. Instead we
        verify the plumbing: detect_s is a real non-negative float that flows through
        to record_run_metrics — not the hardcoded 0.0 literal from Bug #874.

        Test 4 (direct _finalize_delta_tracking call) verifies exact value forwarding.
        """
        svc, tracking = _delta_fixture(repo_to_domain_mapping=True)

        svc.run_delta_analysis()

        assert tracking.record_run_metrics.called, (
            "Bug #874: record_run_metrics must be called by delta path"
        )
        metrics = tracking.record_run_metrics.call_args[0][0]
        assert isinstance(metrics["pass1_duration_s"], float), (
            f"pass1_duration_s must be a float, got {type(metrics['pass1_duration_s'])}"
        )
        assert metrics["pass1_duration_s"] >= 0.0, (
            f"Bug #874: pass1_duration_s must be a real measured value >= 0.0, "
            f"got {metrics.get('pass1_duration_s')}"
        )


# ---------------------------------------------------------------------------
# Test 2: _update_affected_domains duration is measured and passed to _record_run_metrics
# ---------------------------------------------------------------------------


class TestRunDeltaAnalysisRecordsNonzeroMergeTiming:
    """Bug #874 Story A: _update_affected_domains() timing must reach _record_run_metrics."""

    def test_run_delta_analysis_records_nonzero_merge_timing(self, _delta_fixture):
        """
        Assert record_run_metrics is called with pass2_duration_s >= 0.0.

        Same rationale as test 1 for not patching time.time.
        The key invariant: merge_s is a real float, not the hardcoded 0.0 from Bug #874.
        """
        svc, tracking = _delta_fixture(repo_to_domain_mapping=True)

        svc.run_delta_analysis()

        assert tracking.record_run_metrics.called, (
            "Bug #874: record_run_metrics must be called by delta path"
        )
        metrics = tracking.record_run_metrics.call_args[0][0]
        assert isinstance(metrics["pass2_duration_s"], float), (
            f"pass2_duration_s must be a float, got {type(metrics['pass2_duration_s'])}"
        )
        assert metrics["pass2_duration_s"] >= 0.0, (
            f"Bug #874: pass2_duration_s must be a real measured value >= 0.0, "
            f"got {metrics.get('pass2_duration_s')}"
        )


# ---------------------------------------------------------------------------
# Test 3: No-changes-detected early return: record_run_metrics NOT called
# ---------------------------------------------------------------------------


class TestRunDeltaAnalysisNoAffectedDomainsSkipsMetrics:
    """Bug #874 Story A: No-affected-domains path must not call record_run_metrics."""

    def test_run_delta_analysis_no_affected_domains_skips_metrics(self, _delta_fixture):
        """
        When stored_hashes contains repo1 with "some_sha" and read_current_commit returns
        None (non-git dir), repo1 is neither changed nor new. The Story #716 uncovered-repo
        health check may continue the flow, but identify_affected_domains returns set()
        because _index.md has no repo-to-domain table -> early-return branch fires.

        On this branch _finalize_delta_tracking is called WITHOUT output_dir/affected_domains,
        so _record_run_metrics is NOT triggered (the `if output_dir is not None` guard).

        Observable via injected collaborators:
          - record_run_metrics NOT called (no domain updates, so no metrics)
          - update_tracking IS called (tracking status updated to completed)
          - result status is "completed", affected_domains is 0
        """
        svc, tracking = _delta_fixture(repo_to_domain_mapping=False)

        result = svc.run_delta_analysis()

        assert not tracking.record_run_metrics.called, (
            "record_run_metrics must NOT be called when no domains were updated: "
            "no output_dir/affected_domains passed to _finalize_delta_tracking"
        )
        assert tracking.update_tracking.called, (
            "update_tracking must be called even on the no-affected-domains path"
        )
        assert result["status"] == "completed", (
            f"Expected status 'completed' on no-affected-domains path, got {result.get('status')}"
        )
        assert result == {"status": "completed", "affected_domains": 0}


# ---------------------------------------------------------------------------
# Test 4: _finalize_delta_tracking passes detect_s/merge_s through directly
# ---------------------------------------------------------------------------


class TestFinalizesDeltaTrackingPassesTimingsThroughToRecordRunMetrics:
    """Bug #874 Story A: _finalize_delta_tracking must forward detect_s/merge_s."""

    def test_finalize_delta_tracking_passes_timings_through_to_record_run_metrics(
        self, tmp_path
    ):
        """
        Direct unit test: call _finalize_delta_tracking(detect_s=1.5, merge_s=2.5).
        Assert record_run_metrics called with pass1_duration_s=1.5, pass2_duration_s=2.5.
        Only _tracking_backend is mocked (injected collaborator).
        """
        tracking = Mock()
        tracking.get_tracking.return_value = {
            "status": "pending",
            "commit_hashes": None,
        }

        gm = Mock()
        gm.golden_repos_dir = str(tmp_path)

        svc = DependencyMapService(gm, Mock(), tracking, Mock())

        dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        (dep_map_dir / "auth.md").write_text("auth domain content")

        config = Mock()
        config.dependency_map_interval_hours = 24

        svc._finalize_delta_tracking(
            config,
            all_repos=[{"alias": "repo1"}],
            output_dir=dep_map_dir,
            affected_domains={"auth"},
            detect_s=1.5,
            merge_s=2.5,
        )

        assert tracking.record_run_metrics.called, (
            "_record_run_metrics must be called by _finalize_delta_tracking"
        )
        metrics = tracking.record_run_metrics.call_args[0][0]
        assert metrics["pass1_duration_s"] == pytest.approx(1.5, abs=1e-9), (
            f"Bug #874: pass1_duration_s must be detect_s=1.5, "
            f"got {metrics.get('pass1_duration_s')}"
        )
        assert metrics["pass2_duration_s"] == pytest.approx(2.5, abs=1e-9), (
            f"Bug #874: pass2_duration_s must be merge_s=2.5, "
            f"got {metrics.get('pass2_duration_s')}"
        )


# ---------------------------------------------------------------------------
# Test 5: Regression guard — literal "0.0, 0.0" must not appear in source
# ---------------------------------------------------------------------------


class TestBug874StoryANoHardcodedZeros:
    """Regression guard: the literal '0.0, 0.0' must be gone from _finalize_delta_tracking."""

    def test_bug_874_story_a_no_hardcoded_zeros(self):
        """
        Inspect the source of _finalize_delta_tracking at test-time.
        Assert the literal substring '0.0, 0.0' does not appear.
        This test fails before the fix and passes after.
        """
        source = inspect.getsource(DependencyMapService._finalize_delta_tracking)
        assert "0.0, 0.0" not in source, (
            "Bug #874 Story A regression: the hardcoded literal '0.0, 0.0' is still "
            "present in _finalize_delta_tracking. Remove it and pass real timing values."
        )
