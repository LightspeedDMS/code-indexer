"""
Unit tests for DependencyMapService sentinel integration (Story #1035).

Tests the SharedJobSentinel wire-up in:
- DependencyMapService.is_available() (Component 1)
- run_full_analysis() / run_delta_analysis() claim/release (Component 2)

Covers AC4, AC5, AC8, AC9, AC10.

Anti-mock philosophy: SharedJobSentinel uses real tmpdir filesystem.
Only the heavy analysis innards (_execute_analysis_passes, _finalize_analysis,
_setup_analysis) are stubbed to keep tests fast and focused on sentinel behavior.
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import (
    AnalysisAlreadyRunningError,
    DependencyMapService,
)
from code_indexer.server.services.shared_job_sentinel import SharedJobSentinel

# ---------------------------------------------------------------------------
# Constants mirrored from the service (must stay in sync with service module)
# ---------------------------------------------------------------------------

ANALYSIS_STALE_TIMEOUT_SECONDS = 14400  # 4 hours — mirrors service constant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> DependencyMapService:
    """Build a minimal DependencyMapService with mock dependencies."""
    gm = MagicMock()
    gm.golden_repos_dir = str(tmp_path)
    gm.list_golden_repos.return_value = []
    gm.get_actual_repo_path.return_value = str(tmp_path / "cidx-meta")

    tracking = MagicMock()
    tracking.get_tracking.return_value = {"status": "pending", "commit_hashes": None}

    config_mgr = MagicMock()
    ci_config = MagicMock()
    ci_config.dependency_map_enabled = True
    ci_config.dependency_map_interval_hours = 168  # int required by timedelta()
    config_mgr.get_claude_integration_config.return_value = ci_config

    analyzer = MagicMock()

    return DependencyMapService(
        golden_repos_manager=gm,
        config_manager=config_mgr,
        tracking_backend=tracking,
        analyzer=analyzer,
    )


def _sentinel_dir(tmp_path: Path) -> Path:
    """Return the sentinel directory that DependencyMapService will use."""
    return tmp_path / "cidx-meta" / "dependency-map"


def _make_sentinel(tmp_path: Path) -> SharedJobSentinel:
    """Build a SharedJobSentinel pointing at the same dir DependencyMapService uses."""
    d = _sentinel_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    return SharedJobSentinel(d, stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS)


def _stub_analysis_body(
    svc: DependencyMapService, result: Optional[Dict[str, Any]] = None
) -> None:
    """
    Stub _setup_analysis and _execute_analysis_passes so run_full_analysis
    returns quickly without real analysis work.
    """
    if result is None:
        result = {
            "early_return": False,
            "config": MagicMock(dependency_map_enabled=True),
            "paths": {
                "golden_repos_root": Path("/tmp/gr"),
                "cidx_meta_path": Path("/tmp/gr/cidx-meta"),
                "cidx_meta_read_path": Path("/tmp/gr/cidx-meta"),
                "staging_dir": Path("/tmp/gr/cidx-meta/dependency-map.staging"),
                "final_dir": Path("/tmp/gr/cidx-meta/dependency-map"),
            },
            "repo_list": [{"alias": "repo1", "clone_path": "/tmp/repo1"}],
        }
    svc._setup_analysis = MagicMock(return_value=result)  # type: ignore[method-assign]
    svc._execute_analysis_passes = MagicMock(  # type: ignore[method-assign]
        return_value=([], [], 0.1, 0.1)
    )
    svc._finalize_analysis = MagicMock()  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Component 1: is_available() sentinel rewire
# ---------------------------------------------------------------------------


class TestIsAvailableSentinelRewire:
    """is_available() must consult SharedJobSentinel, not threading.Lock."""

    def test_is_available_returns_true_when_no_sentinel(self, tmp_path: Path) -> None:
        """is_available() returns True when no sentinel file exists."""
        svc = _make_service(tmp_path)
        assert svc.is_available() is True

    def test_is_available_returns_false_when_sentinel_held(
        self, tmp_path: Path
    ) -> None:
        """is_available() returns False when a fresh sentinel file exists."""
        svc = _make_service(tmp_path)
        snt = _make_sentinel(tmp_path)
        snt.try_claim("analysis", "other-job", "other-node")

        assert svc.is_available() is False

    def test_is_available_returns_true_when_sentinel_is_stale(
        self, tmp_path: Path
    ) -> None:
        """is_available() returns True when the sentinel is older than ANALYSIS_STALE_TIMEOUT."""
        svc = _make_service(tmp_path)
        # Pre-create a stale sentinel (5 hours old)
        sentinel_dir = _sentinel_dir(tmp_path)
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        stale_payload = {
            "op_type": "analysis",
            "job_id": "stale-job",
            "node_id": "crashed-node",
            "started_at": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
        }
        (sentinel_dir / "_active_analysis.lock").write_text(json.dumps(stale_payload))

        assert svc.is_available() is True


# ---------------------------------------------------------------------------
# Component 2: run_full_analysis sentinel claim/release
# ---------------------------------------------------------------------------


class TestRunFullAnalysisSentinel:
    """run_full_analysis() must wrap body in sentinel try_claim / release."""

    def test_run_full_analysis_claims_sentinel_before_work(
        self, tmp_path: Path
    ) -> None:
        """Sentinel file exists while run_full_analysis is executing."""
        svc = _make_service(tmp_path)
        sentinel_dir = _sentinel_dir(tmp_path)
        claimed_during: list[bool] = []

        def fake_execute_passes(config, paths, repo_list, tracked_job_id=None):
            snt = SharedJobSentinel(
                sentinel_dir, stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS
            )
            active = snt.read_active("analysis")
            claimed_during.append(active is not None)
            return [], [], 0.1, 0.1

        _stub_analysis_body(svc)
        svc._execute_analysis_passes = fake_execute_passes  # type: ignore[method-assign]

        svc.run_full_analysis(job_id="job-test-001")

        assert claimed_during == [True], "Sentinel must be held during analysis body"

    def test_run_full_analysis_releases_sentinel_in_finally_on_success(
        self, tmp_path: Path
    ) -> None:
        """Sentinel is released after successful run_full_analysis."""
        svc = _make_service(tmp_path)
        _stub_analysis_body(svc)

        svc.run_full_analysis(job_id="job-success-001")

        assert _make_sentinel(tmp_path).read_active("analysis") is None

    def test_run_full_analysis_releases_sentinel_in_finally_on_exception(
        self, tmp_path: Path
    ) -> None:
        """Sentinel is released even when run_full_analysis raises."""
        svc = _make_service(tmp_path)
        _stub_analysis_body(svc)
        svc._execute_analysis_passes = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("simulated analysis failure")
        )

        with pytest.raises(RuntimeError, match="simulated analysis failure"):
            svc.run_full_analysis(job_id="job-fail-001")

        # Sentinel must be gone despite exception
        assert _make_sentinel(tmp_path).read_active("analysis") is None

    def test_run_full_analysis_raises_analysis_already_running_when_claimed(
        self, tmp_path: Path
    ) -> None:
        """run_full_analysis raises AnalysisAlreadyRunningError when sentinel already held."""
        svc = _make_service(tmp_path)
        snt = _make_sentinel(tmp_path)
        snt.try_claim("analysis", "other-node-job", "other-node")

        with pytest.raises(AnalysisAlreadyRunningError) as exc_info:
            svc.run_full_analysis(job_id="my-job-002")

        assert exc_info.value.active_job_id == "other-node-job"

    def test_run_full_analysis_sentinel_holds_correct_job_id(
        self, tmp_path: Path
    ) -> None:
        """Sentinel file contains the job_id passed to run_full_analysis."""
        svc = _make_service(tmp_path)
        sentinel_dir = _sentinel_dir(tmp_path)
        captured_job_id: list[str] = []

        def fake_execute_passes(config, paths, repo_list, tracked_job_id=None):
            snt = SharedJobSentinel(
                sentinel_dir, stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS
            )
            active = snt.read_active("analysis")
            if active:
                captured_job_id.append(active.job_id)
            return [], [], 0.1, 0.1

        _stub_analysis_body(svc)
        svc._execute_analysis_passes = fake_execute_passes  # type: ignore[method-assign]

        my_job_id = "explicit-job-abc"
        svc.run_full_analysis(job_id=my_job_id)

        assert captured_job_id == [my_job_id]


# ---------------------------------------------------------------------------
# Component 2: run_delta_analysis sentinel claim/release
# ---------------------------------------------------------------------------


class TestRunDeltaAnalysisSentinel:
    """run_delta_analysis() must use same sentinel behavior as run_full_analysis."""

    def test_run_delta_analysis_claims_sentinel_before_work(
        self, tmp_path: Path
    ) -> None:
        """Sentinel file exists while run_delta_analysis is executing."""
        svc = _make_service(tmp_path)
        sentinel_dir = _sentinel_dir(tmp_path)
        claimed_during: list[bool] = []

        def fake_detect_changes():
            snt = SharedJobSentinel(
                sentinel_dir, stale_timeout_seconds=ANALYSIS_STALE_TIMEOUT_SECONDS
            )
            active = snt.read_active("analysis")
            claimed_during.append(active is not None)
            return [], [], []

        svc.detect_changes = fake_detect_changes  # type: ignore[method-assign]

        svc.run_delta_analysis(job_id="delta-job-001")

        assert claimed_during == [True], "Sentinel must be held during delta body"

    def test_run_delta_analysis_releases_sentinel_on_success(
        self, tmp_path: Path
    ) -> None:
        """Sentinel is released after successful run_delta_analysis."""
        svc = _make_service(tmp_path)
        svc.detect_changes = MagicMock(return_value=([], [], []))  # type: ignore[method-assign]

        svc.run_delta_analysis(job_id="delta-success-001")

        assert _make_sentinel(tmp_path).read_active("analysis") is None

    def test_run_delta_analysis_releases_sentinel_on_exception(
        self, tmp_path: Path
    ) -> None:
        """Sentinel is released even when run_delta_analysis raises."""
        svc = _make_service(tmp_path)
        svc.detect_changes = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("simulated delta failure")
        )

        with pytest.raises(RuntimeError, match="simulated delta failure"):
            svc.run_delta_analysis(job_id="delta-fail-001")

        assert _make_sentinel(tmp_path).read_active("analysis") is None

    def test_run_delta_analysis_raises_analysis_already_running_when_claimed(
        self, tmp_path: Path
    ) -> None:
        """run_delta_analysis raises AnalysisAlreadyRunningError when sentinel already held."""
        svc = _make_service(tmp_path)
        snt = _make_sentinel(tmp_path)
        snt.try_claim("analysis", "other-node-job-delta", "other-node")

        with pytest.raises(AnalysisAlreadyRunningError) as exc_info:
            svc.run_delta_analysis(job_id="my-delta-job")

        assert exc_info.value.active_job_id == "other-node-job-delta"


# ---------------------------------------------------------------------------
# AC8: Concurrent claim race — only one winner
# ---------------------------------------------------------------------------


class TestConcurrentSentinelRace:
    """Two DependencyMapService instances sharing same sentinel dir — one wins."""

    def test_concurrent_runs_only_one_wins(self, tmp_path: Path) -> None:
        """
        Simulate two cluster nodes starting run_full_analysis simultaneously.
        Exactly one must succeed; the other must raise AnalysisAlreadyRunningError.

        Design: svc_a wins the sentinel claim (starts first, pauses in body).
        While svc_a holds the sentinel, svc_b tries to claim and must lose.
        """
        svc_a = _make_service(tmp_path)
        svc_b = _make_service(tmp_path)

        _stub_analysis_body(svc_a)
        _stub_analysis_body(svc_b)

        # svc_a holds the sentinel during body execution until we signal release
        a_entered = threading.Event()
        release_a = threading.Event()

        def slow_execute_a(config, paths, repo_list, tracked_job_id=None):
            a_entered.set()  # signal: svc_a now holds the sentinel
            release_a.wait(timeout=5)  # hold until test releases it
            return [], [], 0.1, 0.1

        svc_a._execute_analysis_passes = slow_execute_a  # type: ignore[method-assign]

        successes: list[str] = []
        failures: list[AnalysisAlreadyRunningError] = []
        other_errors: list[Exception] = []

        def run_a():
            try:
                svc_a.run_full_analysis(job_id="node-a-job")
                successes.append("a")
            except AnalysisAlreadyRunningError as e:
                failures.append(e)
            except Exception as e:
                other_errors.append(e)

        def run_b():
            # Wait until svc_a has the sentinel, then try to claim
            a_entered.wait(timeout=5)
            try:
                svc_b.run_full_analysis(job_id="node-b-job")
                successes.append("b")
            except AnalysisAlreadyRunningError as e:
                failures.append(e)
            except Exception as e:
                other_errors.append(e)

        t_a = threading.Thread(target=run_a)
        t_b = threading.Thread(target=run_b)
        t_a.start()
        t_b.start()

        # Let svc_b attempt its claim, then release svc_a
        t_b.join(timeout=5)  # svc_b should fail fast once it sees the sentinel
        release_a.set()
        t_a.join(timeout=10)

        assert not other_errors, f"Unexpected errors: {other_errors}"
        assert len(successes) == 1, f"Expected exactly 1 winner, got: {successes}"
        assert len(failures) == 1, f"Expected exactly 1 loser, got: {failures}"
        assert failures[0].active_job_id == "node-a-job"

    def test_concurrent_runs_sentinel_clean_after_completion(
        self, tmp_path: Path
    ) -> None:
        """After concurrent race resolves, sentinel is released by the winner."""
        svc_a = _make_service(tmp_path)
        svc_b = _make_service(tmp_path)
        _stub_analysis_body(svc_a)
        _stub_analysis_body(svc_b)

        errors: list[Exception] = []

        def run(svc, job_id):
            try:
                svc.run_full_analysis(job_id=job_id)
            except AnalysisAlreadyRunningError:
                pass  # Expected for loser
            except Exception as e:
                errors.append(e)

        t_a = threading.Thread(target=run, args=(svc_a, "concurrent-a"))
        t_b = threading.Thread(target=run, args=(svc_b, "concurrent-b"))
        t_a.start()
        t_b.start()
        t_a.join(timeout=10)
        t_b.join(timeout=10)

        assert not errors, f"Unexpected errors: {errors}"
        # Sentinel must be clean after all threads done
        assert _make_sentinel(tmp_path).read_active("analysis") is None
