"""Story #927 Phase 2: _try_fire_scheduled_delta control-flow path tests.

Tests three control-flow paths:
- Decision lock not acquired → log + return early (job_tracker NOT called)
- In-flight guard triggers → log + return early (job_tracker NOT called)
- Both gates pass → log + delegate to run_delta_analysis (job_tracker IS called)

Observable effects verified through the injected job_tracker collaborator
(register_job_if_no_conflict), not by patching SUT methods.
"""

import logging
import threading
from typing import cast
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import TrackedJob


def _make_service(job_tracker=None, tmp_path=None):
    """Create DependencyMapService with minimal mocked dependencies."""
    config = MagicMock()
    config.dependency_map_enabled = True
    # Set numeric fields used by run_delta_analysis to avoid TypeError in timedelta()
    config.dependency_map_interval_hours = 24
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = MagicMock()
    golden_repos_manager.list_golden_repos.return_value = []
    golden_repos_manager.golden_repos_dir = (
        str(tmp_path) if tmp_path else "/nonexistent"
    )

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=MagicMock(get_tracking=MagicMock(return_value={})),
        analyzer=MagicMock(),
        job_tracker=job_tracker,
    )


def _make_active_job(operation_type: str) -> TrackedJob:
    """Return a minimal TrackedJob representing an in-flight job."""
    return TrackedJob(
        job_id="in-flight-id",
        operation_type=operation_type,
        status="running",
        username="system",
    )


def _make_job_tracker(active_jobs=None):
    """Return a job_tracker mock with configurable active jobs."""
    mock_tracker = MagicMock()
    mock_tracker.get_active_jobs.return_value = active_jobs or []
    return mock_tracker


def _hold_decision_lock(service: DependencyMapService, key: str) -> threading.Lock:
    """Pre-acquire the per-key solo decision lock to simulate contention.

    Returns the held Lock so callers can release it in a finally block.
    """
    service._solo_decision_locks[key] = threading.Lock()
    service._solo_decision_locks[key].acquire()
    # cast is safe: we assigned a threading.Lock to this key two lines above;
    # _solo_decision_locks is typed Dict[str, Any] so the lookup returns Any.
    return cast(threading.Lock, service._solo_decision_locks[key])


class TestTryFireScheduledDelta:
    """_try_fire_scheduled_delta: three control-flow paths.

    Observable effects verified via job_tracker.register_job_if_no_conflict
    (called by run_delta_analysis when both gates pass, not called otherwise).
    """

    def test_skips_when_decision_lock_not_acquired(self, caplog, tmp_path):
        """When decision lock is contended, logs skipped and job_tracker is not called."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        service = _make_service(job_tracker=mock_tracker, tmp_path=tmp_path)
        held_lock = _hold_decision_lock(service, "delta")

        try:
            with caplog.at_level(logging.INFO):
                service._try_fire_scheduled_delta()
        finally:
            held_lock.release()

        mock_tracker.register_job_if_no_conflict.assert_not_called()
        assert "scheduled_delta_skipped_decision_lock_held" in caplog.text

    def test_skips_when_in_flight_guard_triggers(self, caplog, tmp_path):
        """When a dep-map job is in-flight, logs skipped and job_tracker is not called."""
        mock_tracker = _make_job_tracker(
            active_jobs=[_make_active_job("dependency_map_delta")]
        )
        service = _make_service(job_tracker=mock_tracker, tmp_path=tmp_path)

        with caplog.at_level(logging.INFO):
            service._try_fire_scheduled_delta()

        mock_tracker.register_job_if_no_conflict.assert_not_called()
        assert "scheduled_delta_skipped_reentrance" in caplog.text

    def test_fires_when_both_gates_pass(self, caplog, tmp_path):
        """When lock acquired and no in-flight job, run_delta_analysis executes and logs fired.

        Verified via job_tracker.register_job_if_no_conflict being called,
        which run_delta_analysis invokes when both gates are clear.
        """
        mock_tracker = _make_job_tracker(active_jobs=[])
        service = _make_service(job_tracker=mock_tracker, tmp_path=tmp_path)

        with caplog.at_level(logging.INFO):
            service._try_fire_scheduled_delta()

        mock_tracker.register_job_if_no_conflict.assert_called_once()
        assert "scheduled_delta_fired" in caplog.text

    def test_decision_lock_released_before_job_tracker_called(self, tmp_path):
        """Decision lock must be released before run_delta_analysis delegates to job_tracker.

        Verified by inspecting lock state inside register_job_if_no_conflict:
        if the 'delta' solo lock can be acquired non-blockingly, it is released.
        """
        lock_state_during_register: list[bool] = []

        def fake_register(**kwargs):
            delta_lock = service._solo_decision_locks.get("delta")
            if delta_lock is not None:
                can_acquire = delta_lock.acquire(blocking=False)
                if can_acquire:
                    delta_lock.release()
                lock_state_during_register.append(can_acquire)
            else:
                lock_state_during_register.append(True)

        mock_tracker = _make_job_tracker(active_jobs=[])
        mock_tracker.register_job_if_no_conflict.side_effect = fake_register
        service = _make_service(job_tracker=mock_tracker, tmp_path=tmp_path)

        service._try_fire_scheduled_delta()

        assert lock_state_during_register == [True], (
            "Decision lock was still held when run_delta_analysis delegated to job_tracker"
        )


def _make_refinement_service(job_tracker=None, tmp_path=None):
    """Create DependencyMapService configured for refinement-path tests.

    Sets refinement_enabled=True so run_refinement_cycle proceeds past its
    early-exit guard. The observable is config_manager.get_claude_integration_config
    call count — run_refinement_cycle calls it immediately on entry.
    """
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.refinement_enabled = True
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    tracking_backend = MagicMock(get_tracking=MagicMock(return_value={}))

    golden_repos_manager = MagicMock()
    golden_repos_manager.list_golden_repos.return_value = []
    golden_repos_manager.golden_repos_dir = (
        str(tmp_path) if tmp_path else "/nonexistent"
    )

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=MagicMock(),
        job_tracker=job_tracker,
    )
    return service, config_manager


class TestTryFireScheduledRefinement:
    """_try_fire_scheduled_refinement: three control-flow paths.

    Observable effect: config_manager.get_claude_integration_config is called by
    run_refinement_cycle immediately on entry (before any early exits). When the
    helper skips before calling run_refinement_cycle, the call count stays the same.
    """

    def test_skips_when_decision_lock_not_acquired(self, caplog, tmp_path):
        """When decision lock is contended, logs skipped and run_refinement_cycle is not called."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        service, config_manager = _make_refinement_service(
            job_tracker=mock_tracker, tmp_path=tmp_path
        )
        held_lock = _hold_decision_lock(service, "refinement")
        calls_before = config_manager.get_claude_integration_config.call_count

        try:
            with caplog.at_level(logging.INFO):
                service._try_fire_scheduled_refinement()
        finally:
            held_lock.release()

        assert config_manager.get_claude_integration_config.call_count == calls_before
        assert "scheduled_refinement_skipped_decision_lock_held" in caplog.text

    def test_skips_when_in_flight_guard_triggers(self, caplog, tmp_path):
        """When a dep-map job is in-flight, logs skipped and run_refinement_cycle is not called."""
        mock_tracker = _make_job_tracker(
            active_jobs=[_make_active_job("dependency_map_refinement")]
        )
        service, config_manager = _make_refinement_service(
            job_tracker=mock_tracker, tmp_path=tmp_path
        )
        calls_before = config_manager.get_claude_integration_config.call_count

        with caplog.at_level(logging.INFO):
            service._try_fire_scheduled_refinement()

        assert config_manager.get_claude_integration_config.call_count == calls_before
        assert "scheduled_refinement_skipped_reentrance" in caplog.text

    def test_fires_when_both_gates_pass(self, caplog, tmp_path):
        """When lock acquired and no in-flight job, run_refinement_cycle executes and logs fired.

        Verified via config_manager.get_claude_integration_config call count increasing —
        run_refinement_cycle calls it on entry before checking for _domains.json.
        """
        mock_tracker = _make_job_tracker(active_jobs=[])
        service, config_manager = _make_refinement_service(
            job_tracker=mock_tracker, tmp_path=tmp_path
        )
        calls_before = config_manager.get_claude_integration_config.call_count

        with caplog.at_level(logging.INFO):
            service._try_fire_scheduled_refinement()

        assert config_manager.get_claude_integration_config.call_count > calls_before
        assert "scheduled_refinement_fired" in caplog.text


# ---------------------------------------------------------------------------
# Story #927 Phase 3: scheduler trigger sites wire to _maybe_run_auto_repair
# ---------------------------------------------------------------------------


import pytest  # noqa: E402 — must follow existing imports at module top


def _make_auto_repair_service(tmp_path):
    """Service with dep_map_auto_repair_enabled=True, anomalies, and a repair_fn spy."""
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dep_map_auto_repair_enabled = True
    config.refinement_enabled = True
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    health_report = MagicMock()
    health_report.anomalies = [MagicMock(), MagicMock()]
    repair_fn = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=MagicMock(
            list_golden_repos=MagicMock(return_value=[]),
            golden_repos_dir=str(tmp_path),
        ),
        config_manager=config_manager,
        tracking_backend=MagicMock(get_tracking=MagicMock(return_value={})),
        analyzer=MagicMock(),
        job_tracker=_make_job_tracker(active_jobs=[]),
        repair_invoker_fn=repair_fn,
        health_check_fn=MagicMock(return_value=health_report),
    )
    return service, repair_fn


class TestSchedulerWiresAutoRepair:
    """After scheduler fires successfully, repair_invoker_fn is invoked via auto-repair helper."""

    @pytest.mark.parametrize(
        "trigger_method, fired_log",
        [
            ("_try_fire_scheduled_delta", "scheduled_delta_fired"),
            ("_try_fire_scheduled_refinement", "scheduled_refinement_fired"),
        ],
    )
    def test_scheduler_fires_auto_repair_after_success(
        self, tmp_path, caplog, trigger_method, fired_log
    ):
        """After scheduler helper fires, both the fired log and repair_fn call are observed."""
        service, repair_fn = _make_auto_repair_service(tmp_path)

        with caplog.at_level(logging.INFO):
            getattr(service, trigger_method)()

        assert fired_log in caplog.text, f"Expected '{fired_log}' in logs"
        repair_fn.assert_called_once()
