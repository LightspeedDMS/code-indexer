"""
RED-phase unit tests for Story #876 Phase B-2 Deliverable 2.

DepMapRepairExecutor MUST fork the repair run into two independent branches:
  Branch A (dep_map): existing phases 0->5 over HealthReport.anomalies.
  Branch B (lifecycle): LifecycleBatchRunner.run(aliases, parent_job_id).

Progress is reported per branch (NO unified overall_percent).  The executor
emits at least one progress event whose numeric value is the branch-separated
sentinel (``BRANCH_PROGRESS_SENTINEL``) and whose ``info`` payload is a JSON
object with top-level keys ``dep_map`` and ``lifecycle``.  Lifecycle-branch
failures MUST be captured in ``RepairResult.errors`` without swallowing the
dep_map branch.

MOCK BOUNDARY MAP
-----------------
  REAL   : DepMapRepairExecutor, DepMapHealthDetector, IndexRegenerator,
           HealthReport, RepairResult, filesystem output_dir.
  MOCKED : LifecycleBatchRunner -- patched at the executor's USE-SITE
           (``_RUNNER_PATCH_TARGET`` below), never at its import source.

Test discipline
---------------
  * Each test body <= 30 lines.
  * All setup through helpers in ``_fork_join_fixtures``.
  * No inline magic numbers -- use ``BRANCH_PROGRESS_SENTINEL`` via helpers.
  * No inline executor construction -- use ``_make_skip_executor`` or
    ``_make_wired_executor``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code_indexer.server.services.dep_map_health_detector import Anomaly
from code_indexer.server.services.dep_map_repair_executor import RepairResult

from tests.unit.server.services._fork_join_fixtures import (
    EMPTY_LIFECYCLE,
    MISSING_INDEX_ANOMALY_TYPE,
    MULTI_LIFECYCLE,
    SINGLE_LIFECYCLE,
    _assert_graceful_skip_result,
    _branch_progress_events,
    _capture_progress,
    _make_golden_repos_dir,
    _make_skip_executor,
    _make_wired_executor,
    _setup_lifecycle_context,
)


# Mock-at-USE-SITE target.  LifecycleBatchRunner is imported INTO the
# executor module for its fork/join branch B; patching at the symbol's
# original source would leak the patch across unrelated consumers.
_RUNNER_PATCH_TARGET: str = (
    "code_indexer.server.services.dep_map_repair_executor.LifecycleBatchRunner"
)


# ---------------------------------------------------------------------------
# (a) Combined work: both branches run
# ---------------------------------------------------------------------------


def test_fork_join_spawns_both_branches_when_both_have_work(tmp_path: Path) -> None:
    """Anomalies + lifecycle both non-empty -> both branches execute."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=MULTI_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )
    golden_repos_dir = _make_golden_repos_dir(tmp_path)

    with patch(_RUNNER_PATCH_TARGET) as mock_runner_cls:
        executor = _make_wired_executor(golden_repos_dir)
        executor.execute(output_dir, report, parent_job_id="job-777")

    mock_runner_cls.assert_called_once()
    runner_instance = mock_runner_cls.return_value
    runner_instance.run.assert_called_once_with(
        list(MULTI_LIFECYCLE), parent_job_id="job-777"
    )


# ---------------------------------------------------------------------------
# (b) Skip lifecycle when list empty
# ---------------------------------------------------------------------------


def test_fork_join_skips_lifecycle_branch_when_list_empty(tmp_path: Path) -> None:
    """Empty lifecycle list -> runner not invoked AND dep_map completes cleanly."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=EMPTY_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )
    golden_repos_dir = _make_golden_repos_dir(tmp_path)

    with patch(_RUNNER_PATCH_TARGET) as mock_runner_cls:
        executor = _make_wired_executor(golden_repos_dir)
        result = executor.execute(output_dir, report, parent_job_id="job-1")

    mock_runner_cls.assert_not_called()
    _assert_graceful_skip_result(result)


# ---------------------------------------------------------------------------
# (c) Skip lifecycle when invoker not wired
# ---------------------------------------------------------------------------


def test_fork_join_skips_lifecycle_branch_when_invoker_not_wired(
    tmp_path: Path,
) -> None:
    """lifecycle_invoker=None -> runner not invoked AND graceful RepairResult."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=SINGLE_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )
    golden_repos_dir = _make_golden_repos_dir(tmp_path)

    with patch(_RUNNER_PATCH_TARGET) as mock_runner_cls:
        executor = _make_skip_executor(golden_repos_dir=golden_repos_dir)
        result = executor.execute(output_dir, report, parent_job_id="job-2")

    mock_runner_cls.assert_not_called()
    _assert_graceful_skip_result(result)


# ---------------------------------------------------------------------------
# (d) Skip lifecycle when golden_repos_dir not wired
# ---------------------------------------------------------------------------


def test_fork_join_skips_lifecycle_when_golden_repos_dir_not_wired(
    tmp_path: Path,
) -> None:
    """golden_repos_dir=None -> runner not invoked AND graceful RepairResult."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=SINGLE_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )

    with patch(_RUNNER_PATCH_TARGET) as mock_runner_cls:
        executor = _make_skip_executor(golden_repos_dir=None)
        result = executor.execute(output_dir, report, parent_job_id="job-3")

    mock_runner_cls.assert_not_called()
    _assert_graceful_skip_result(result)


# ---------------------------------------------------------------------------
# (e) Branch-separated progress payload
# ---------------------------------------------------------------------------


def test_fork_join_emits_branch_separated_progress_payload(tmp_path: Path) -> None:
    """Both branches active -> at least one sentinel-valued event carries
    JSON info with top-level dep_map and lifecycle keys."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=MULTI_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )
    golden_repos_dir = _make_golden_repos_dir(tmp_path)
    callback, captured = _capture_progress()

    with patch(_RUNNER_PATCH_TARGET):
        executor = _make_wired_executor(golden_repos_dir, progress_callback=callback)
        executor.execute(output_dir, report, parent_job_id="job-4")

    branch_events = _branch_progress_events(captured)
    assert branch_events, "expected at least one branch-separated progress event"
    assert any("dep_map" in ev and "lifecycle" in ev for ev in branch_events), (
        f"no event carried both dep_map and lifecycle keys: {branch_events!r}"
    )


# ---------------------------------------------------------------------------
# (f) Lifecycle exception does not swallow dep_map branch
# ---------------------------------------------------------------------------


def test_fork_join_lifecycle_exception_does_not_swallow_dep_map(
    tmp_path: Path,
) -> None:
    """Lifecycle raises -> dep_map branch STILL completes Phase 5 AND error captured."""
    output_dir, report = _setup_lifecycle_context(
        tmp_path,
        lifecycle=MULTI_LIFECYCLE,
        anomalies=[Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)],
    )
    golden_repos_dir = _make_golden_repos_dir(tmp_path)

    with patch(_RUNNER_PATCH_TARGET) as mock_runner_cls:
        mock_runner_cls.return_value.run.side_effect = RuntimeError("boom")
        executor = _make_wired_executor(golden_repos_dir)
        result = executor.execute(output_dir, report, parent_job_id="job-5")

    assert isinstance(result, RepairResult), (
        f"expected RepairResult, got {type(result)}"
    )
    assert any(
        "lifecycle" in e.lower() and "boom" in e.lower() for e in result.errors
    ), f"expected lifecycle/boom error, got {result.errors!r}"
    assert result.final_health_status not in ("", "unknown"), (
        f"Phase 5 did not run: final_health_status={result.final_health_status!r}"
    )
