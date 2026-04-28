"""Story #927 Phase 3 AC7: manual triggers do NOT invoke auto-repair.

AC7: When run_delta_analysis() or run_refinement_cycle() is called directly
(operator/REST/MCP), _maybe_run_auto_repair_after_scheduled is NOT invoked.
Only _try_fire_scheduled_delta and _try_fire_scheduled_refinement call it.

Verified by asserting repair_invoker_fn is never called after a direct manual
invocation of run_delta_analysis or run_refinement_cycle.
"""

from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service_with_repair_fn(tmp_path):
    """Create DependencyMapService with a repair_invoker_fn spy and dep_map enabled."""
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dep_map_auto_repair_enabled = True
    config.refinement_enabled = True
    config.dep_map_fact_check_enabled = False
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = MagicMock()
    golden_repos_manager.list_golden_repos.return_value = []
    golden_repos_manager.golden_repos_dir = str(tmp_path)

    repair_fn = MagicMock()
    health_fn = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=MagicMock(get_tracking=MagicMock(return_value={})),
        analyzer=MagicMock(),
        job_tracker=MagicMock(
            get_active_jobs=MagicMock(return_value=[]),
            register_job_if_no_conflict=MagicMock(return_value="manual-job-id"),
        ),
        repair_invoker_fn=repair_fn,
        health_check_fn=health_fn,
    )
    return service, repair_fn, health_fn


class TestAC7ManualTriggersNoAutoRepair:
    """AC7: Direct calls to run_delta_analysis/run_refinement_cycle skip auto-repair."""

    def test_manual_delta_no_auto_repair(self, tmp_path):
        """run_delta_analysis() does not invoke repair_invoker_fn."""
        service, repair_fn, _health_fn = _make_service_with_repair_fn(tmp_path)
        service.run_delta_analysis()
        repair_fn.assert_not_called()

    def test_manual_refinement_no_auto_repair(self, tmp_path):
        """run_refinement_cycle() does not invoke repair_invoker_fn."""
        service, repair_fn, _health_fn = _make_service_with_repair_fn(tmp_path)
        service.run_refinement_cycle()
        repair_fn.assert_not_called()
