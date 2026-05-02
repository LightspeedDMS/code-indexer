"""Bug #931 regression: scheduled refinement bootstrap gap + JobTracker bypass.

Two cooperating defects rendered the scheduled refinement path non-functional:

Defect 1 (bootstrap gap):
    refinement_next_run was written ONLY inside the scheduler's success branch.
    Manual trigger (run_tracked_refinement -> run_refinement_cycle) never wrote it.
    After a successful manual run the scheduler still saw NULL and never auto-fired.

Defect 2 (JobTracker bypass):
    _try_fire_scheduled_refinement called run_refinement_cycle() directly, bypassing
    run_tracked_refinement() and its JobTracker register/update/complete logic.
    Scheduled runs were invisible in the Job Status panel and /jobs tab.

Fix:
    Defect 1: run_refinement_cycle stamps refinement_next_run on every success.
    Defect 2: _try_fire_scheduled_refinement calls run_tracked_refinement() so every
              scheduled run registers with JobTracker.

Test fixture strategy (constructor injection only, no SUT monkey-patching):
    - All collaborators are injected via the DependencyMapService constructor.
    - _get_cidx_meta_read_path() resolves through the REAL method: the versioned
      directory tmp_path/.versioned/cidx-meta/v_00001/ is created so the real logic
      returns it without any method replacement.
    - analyzer.invoke_refinement_file returns None deterministically, causing
      _refine_existing_domain to return False (no-op) — controls the external CLI
      boundary through the injected collaborator rather than environment state.
    - threading.Lock() is left real; single-threaded test execution always acquires.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_refinement_next_run_calls(mock_tracking):
    """Return all update_tracking calls that included refinement_next_run keyword."""
    return [
        c
        for c in mock_tracking.update_tracking.call_args_list
        if "refinement_next_run" in c.kwargs
    ]


def _build_service(
    tmp_path: Path,
    domains: list,
    refinement_enabled: bool = True,
    refinement_interval_hours: int = 24,
    domains_per_run: int = 2,
    job_tracker=None,
):
    """Build DependencyMapService using only constructor-injected collaborators.

    Filesystem layout:
        tmp_path/.versioned/cidx-meta/v_00001/dependency-map/_domains.json  (read)
        tmp_path/cidx-meta/dependency-map/                                    (write)

    _get_cidx_meta_read_path() resolves to the versioned dir via real logic because
    golden_repos_manager.golden_repos_dir == str(tmp_path).

    analyzer.invoke_refinement_file returns None so _refine_existing_domain returns
    False (no file write, no Claude CLI call) — deterministic external boundary control.
    """
    # Versioned read path (controls _get_cidx_meta_read_path via real logic)
    versioned_dir = tmp_path / ".versioned" / "cidx-meta" / "v_00001"
    dep_map_read = versioned_dir / "dependency-map"
    dep_map_read.mkdir(parents=True, exist_ok=True)
    (dep_map_read / "_domains.json").write_text(json.dumps(domains))

    # Write path (golden_repos_dir/cidx-meta/dependency-map)
    (tmp_path / "cidx-meta" / "dependency-map").mkdir(parents=True, exist_ok=True)

    # Config collaborator
    cfg = MagicMock()
    cfg.refinement_enabled = refinement_enabled
    cfg.refinement_interval_hours = refinement_interval_hours
    cfg.refinement_domains_per_run = domains_per_run
    cfg.dependency_map_interval_hours = 24
    cfg_mgr = MagicMock()
    cfg_mgr.get_claude_integration_config.return_value = cfg

    # Tracking collaborator (assertion target for Defect 1)
    tracking = MagicMock()
    tracking.get_tracking.return_value = {"refinement_cursor": 0}
    tracking.update_tracking.return_value = None

    # Analyzer collaborator: invoke_refinement_file returns None -> no-op refine
    analyzer = MagicMock()
    analyzer.invoke_refinement_file.return_value = None

    # golden_repos_manager drives _get_cidx_meta_read_path path resolution
    golden_repos = MagicMock()
    golden_repos.golden_repos_dir = str(tmp_path)

    service = DependencyMapService(
        golden_repos_manager=golden_repos,
        config_manager=cfg_mgr,
        tracking_backend=tracking,
        analyzer=analyzer,
        refresh_scheduler=None,
        job_tracker=job_tracker,
    )
    return service, tracking


def _make_job_tracker_spy():
    """Return a job_tracker mock that records register/update_status/complete calls."""
    jt = MagicMock()
    jt.get_active_jobs.return_value = []
    jt.check_operation_conflict.return_value = None
    jt.register_job.return_value = None
    jt.update_status.return_value = None
    jt.complete_job.return_value = None
    return jt


# ---------------------------------------------------------------------------
# pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cycle_service(tmp_path):
    """Service + tracking mock for run_refinement_cycle tests (Defect 1)."""
    service, tracking = _build_service(tmp_path, domains=[{"name": "domain-A"}])
    return service, tracking


@pytest.fixture()
def scheduled_service(tmp_path):
    """Service + tracking + job_tracker for _try_fire_scheduled_refinement tests (Defect 2)."""
    jt = _make_job_tracker_spy()
    service, tracking = _build_service(
        tmp_path, domains=[{"name": "domain-A"}], job_tracker=jt
    )
    return service, tracking, jt


# ---------------------------------------------------------------------------
# Class 1: Defect 1 regression — run_refinement_cycle seeds the schedule
# ---------------------------------------------------------------------------


class TestRefinementCycleSeedsSchedule:
    """Defect 1 regression: run_refinement_cycle stamps refinement_next_run on success.

    Pre-fix: only the scheduler success branch wrote it; manual path never did.
    Post-fix: run_refinement_cycle owns the stamp regardless of caller.
    """

    def test_stamps_refinement_next_run_on_success(self, cycle_service):
        """run_refinement_cycle calls update_tracking(refinement_next_run=...) on success."""
        service, tracking = cycle_service

        service.run_refinement_cycle()

        calls = _get_refinement_next_run_calls(tracking)
        assert len(calls) >= 1, (
            "run_refinement_cycle must call update_tracking(refinement_next_run=...). "
            f"Actual calls: {tracking.update_tracking.call_args_list}"
        )

    def test_stamp_is_future_isoformat_using_configured_interval(self, tmp_path):
        """Stamp is a valid ISO timestamp approximately now + refinement_interval_hours."""
        interval_h = 12
        service, tracking = _build_service(
            tmp_path,
            domains=[{"name": "domain-A"}],
            refinement_interval_hours=interval_h,
        )
        before = datetime.now(timezone.utc)

        service.run_refinement_cycle()

        calls = _get_refinement_next_run_calls(tracking)
        assert calls, "update_tracking(refinement_next_run=...) not called"
        stamped_dt = datetime.fromisoformat(calls[-1].kwargs["refinement_next_run"])

        assert stamped_dt > before, (
            f"refinement_next_run ({stamped_dt}) must be strictly after cycle ran"
        )
        assert stamped_dt <= before + timedelta(hours=interval_h + 1), (
            f"refinement_next_run ({stamped_dt}) should be within {interval_h}+1h of now"
        )

    def test_manual_trigger_seeds_schedule(self, cycle_service):
        """Manual trigger (run_tracked_refinement) seeds refinement_next_run.

        Core bug-fix evidence: before the fix, the manual path left next_run NULL.
        """
        service, tracking = cycle_service

        service.run_tracked_refinement()

        calls = _get_refinement_next_run_calls(tracking)
        assert len(calls) >= 1, (
            "Manual trigger must seed refinement_next_run. "
            f"Actual calls: {tracking.update_tracking.call_args_list}"
        )

    def test_no_stamp_when_refinement_disabled(self, tmp_path):
        """When refinement_enabled=False, run_refinement_cycle early-returns without stamp."""
        service, tracking = _build_service(
            tmp_path, domains=[{"name": "domain-A"}], refinement_enabled=False
        )

        service.run_refinement_cycle()

        calls = _get_refinement_next_run_calls(tracking)
        assert len(calls) == 0, (
            "run_refinement_cycle must NOT stamp refinement_next_run when disabled. "
            f"Actual calls: {tracking.update_tracking.call_args_list}"
        )


# ---------------------------------------------------------------------------
# Class 2: Defect 2 regression — scheduled path registers JobTracker entries
# ---------------------------------------------------------------------------


class TestScheduledRefinementJobTrackerVisibility:
    """Defect 2 regression: _try_fire_scheduled_refinement registers JobTracker entries.

    Pre-fix: called run_refinement_cycle() directly, bypassing run_tracked_refinement().
    Post-fix: calls run_tracked_refinement() so JobTracker is always informed.

    Assertions observe the job_tracker external collaborator (injected via constructor).
    """

    def test_registers_job_with_correct_operation_type(self, scheduled_service):
        """_try_fire_scheduled_refinement registers operation_type='dependency_map_refinement'."""
        service, _, jt = scheduled_service

        service._try_fire_scheduled_refinement()

        jt.register_job.assert_called()
        # register_job positional signature: register_job(job_id, operation_type, ...)
        operation_types = [
            c.args[1] if len(c.args) > 1 else c.kwargs.get("operation_type")
            for c in jt.register_job.call_args_list
        ]
        assert any(ot == "dependency_map_refinement" for ot in operation_types), (
            f"Expected operation_type='dependency_map_refinement'. Got: {jt.register_job.call_args_list}"
        )

    def test_updates_status_to_running_and_completes(self, scheduled_service):
        """_try_fire_scheduled_refinement updates status to 'running' and calls complete_job."""
        service, _, jt = scheduled_service

        service._try_fire_scheduled_refinement()

        running_calls = [
            c
            for c in jt.update_status.call_args_list
            if c.kwargs.get("status") == "running"
        ]
        assert len(running_calls) >= 1, (
            f"Expected update_status(status='running'). Got: {jt.update_status.call_args_list}"
        )
        jt.complete_job.assert_called_once()

    def test_scheduled_fire_stamps_refinement_next_run(self, scheduled_service):
        """Scheduled fire stamps refinement_next_run (Defect 1 + 2 together).

        Both fixes together: scheduled run registers JobTracker AND stamps next_run.
        """
        service, tracking, jt = scheduled_service

        service._try_fire_scheduled_refinement()

        calls = _get_refinement_next_run_calls(tracking)
        assert len(calls) >= 1, (
            "Scheduled fire must stamp refinement_next_run. "
            f"Actual: {tracking.update_tracking.call_args_list}"
        )
        jt.register_job.assert_called()
