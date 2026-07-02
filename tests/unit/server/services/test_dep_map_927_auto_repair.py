"""Story #927 Phase 3: _maybe_run_auto_repair_after_scheduled gate tests.

AC6: feature flag gate — disabled -> no repair fires; enabled -> fires when all gates pass.
AC3, AC8, AC9 and additional gates added in subsequent increments.
"""

import logging
import threading
from typing import cast
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import TrackedJob


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_health_report(anomaly_count: int):
    """Return a minimal health report object with .anomalies list."""
    mock_health = MagicMock()
    mock_health.anomalies = [MagicMock() for _ in range(anomaly_count)]
    return mock_health


def _make_job_tracker(active_jobs=None):
    mock_tracker = MagicMock()
    mock_tracker.get_active_jobs.return_value = active_jobs or []
    fake_job = TrackedJob(
        job_id="auto-repair-job-id",
        operation_type="dependency_map_repair",
        status="pending",
        username="system",
    )
    mock_tracker.register_job.return_value = fake_job
    return mock_tracker


def _make_service(
    tmp_path,
    dep_map_auto_repair_enabled: bool = True,
    job_tracker=None,
    health_check_fn=None,
    repair_invoker_fn=None,
):
    """Create DependencyMapService with Phase 3 injectable parameters."""
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dep_map_auto_repair_enabled = dep_map_auto_repair_enabled
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = MagicMock()
    golden_repos_manager.list_golden_repos.return_value = []
    golden_repos_manager.golden_repos_dir = str(tmp_path)

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=MagicMock(get_tracking=MagicMock(return_value={})),
        analyzer=MagicMock(),
        job_tracker=job_tracker,
        repair_invoker_fn=repair_invoker_fn,
        health_check_fn=health_check_fn,
    )


# ---------------------------------------------------------------------------
# AC6: Feature flag gate (first 3 methods)
# ---------------------------------------------------------------------------


class TestAC6FeatureFlagGate:
    """AC6: dep_map_auto_repair_enabled must be True for auto-repair to fire."""

    def test_flag_disabled_no_repair_fires(self, caplog, tmp_path):
        """When dep_map_auto_repair_enabled=False, no repair is attempted."""
        mock_tracker = _make_job_tracker()
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=3))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=False,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.INFO):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_disabled" in caplog.text
        mock_tracker.register_job.assert_not_called()
        repair_fn.assert_not_called()

    def test_flag_enabled_all_gates_pass_fires_repair(self, caplog, tmp_path):
        """When flag enabled, no in-flight, anomalies present — repair fires."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.INFO):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_fired" in caplog.text
        mock_tracker.register_job.assert_called_once()
        repair_fn.assert_called_once()

    def test_flag_enabled_trigger_source_propagated(self, caplog, tmp_path):
        """trigger_source value is present in log record extra fields when repair fires."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=1))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.INFO):
            service._maybe_run_auto_repair_after_scheduled("refinement")

        trigger_values = [getattr(r, "trigger", None) for r in caplog.records]
        assert "refinement" in trigger_values

    def test_flag_disabled_health_fn_not_called(self, tmp_path):
        """When flag disabled, health_check_fn is never invoked."""
        health_fn = MagicMock()
        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=False,
            health_check_fn=health_fn,
        )
        service._maybe_run_auto_repair_after_scheduled("delta")
        health_fn.assert_not_called()


# ---------------------------------------------------------------------------
# AC3: No anomalies gate
# ---------------------------------------------------------------------------


class TestAC3NoAnomaliesGate:
    """AC3: When health check reports zero anomalies, no repair is triggered."""

    def test_zero_anomalies_logs_no_anomalies(self, caplog, tmp_path):
        """When anomaly list is empty, logs scheduled_auto_repair_no_anomalies."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=0))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.INFO):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_no_anomalies" in caplog.text
        mock_tracker.register_job.assert_not_called()
        repair_fn.assert_not_called()

    def test_zero_anomalies_no_job_registered(self, tmp_path):
        """When zero anomalies, no job is registered in job_tracker."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=0))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
        )
        service._maybe_run_auto_repair_after_scheduled("delta")
        mock_tracker.register_job.assert_not_called()

    def test_nonzero_anomalies_passes_gate(self, tmp_path):
        """When anomaly_count > 0, health gate passes and repair is attempted."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=5))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )
        service._maybe_run_auto_repair_after_scheduled("delta")
        repair_fn.assert_called_once()


# ---------------------------------------------------------------------------
# AC8: Health check failure (anti-fallback)
# ---------------------------------------------------------------------------


class TestAC8HealthCheckFailure:
    """AC8: health_check_fn raises -> returns gracefully, logs WARNING, no repair."""

    def test_health_check_exception_logs_warning(self, caplog, tmp_path):
        """When health_check_fn raises, WARNING is logged."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(side_effect=RuntimeError("health check failed"))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.WARNING):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_health_check_failed" in caplog.text

    def test_health_check_exception_no_repair_fired(self, tmp_path):
        """When health_check_fn raises, repair_invoker_fn is never called."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(side_effect=ValueError("connection timeout"))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )
        service._maybe_run_auto_repair_after_scheduled("delta")
        repair_fn.assert_not_called()

    def test_health_check_exception_method_returns_gracefully(self, tmp_path):
        """When health_check_fn raises, the method does not re-raise."""
        health_fn = MagicMock(side_effect=RuntimeError("catastrophic failure"))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            health_check_fn=health_fn,
        )
        # Must not raise
        service._maybe_run_auto_repair_after_scheduled("delta")

    def test_no_health_check_fn_logs_warning_and_skips(self, caplog, tmp_path):
        """When health_check_fn is None, logs warning and skips repair."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=None,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.WARNING):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_skipped_no_health_check_fn" in caplog.text
        repair_fn.assert_not_called()


# ---------------------------------------------------------------------------
# AC9: Repair invoker failure
# ---------------------------------------------------------------------------


def _make_ac9_components(tmp_path):
    """Shared setup for AC9 tests: repair_invoker_fn raises RuntimeError."""
    mock_tracker = _make_job_tracker(active_jobs=[])
    repair_fn = MagicMock(side_effect=RuntimeError("repair start failed"))
    health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))
    service = _make_service(
        tmp_path=tmp_path,
        dep_map_auto_repair_enabled=True,
        job_tracker=mock_tracker,
        health_check_fn=health_fn,
        repair_invoker_fn=repair_fn,
    )
    return mock_tracker, repair_fn, service


class TestAC9RepairInvokerFailure:
    """AC9: repair_invoker_fn raises -> job marked failed, ERROR logged."""

    def test_repair_exception_logs_error(self, caplog, tmp_path):
        """When repair_invoker_fn raises, ERROR is logged."""
        mock_tracker, _repair_fn, service = _make_ac9_components(tmp_path)

        with caplog.at_level(logging.ERROR):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_start_failed" in caplog.text

    def test_repair_exception_job_marked_failed(self, tmp_path):
        """When repair_invoker_fn raises, fail_job is called on the registered job."""
        mock_tracker, _repair_fn, service = _make_ac9_components(tmp_path)
        service._maybe_run_auto_repair_after_scheduled("delta")
        mock_tracker.fail_job.assert_called_once()

    def test_repair_exception_method_returns_gracefully(self, tmp_path):
        """When repair_invoker_fn raises, the method does not re-raise."""
        _mock_tracker, _repair_fn, service = _make_ac9_components(tmp_path)
        # Must not raise
        service._maybe_run_auto_repair_after_scheduled("delta")

    def test_no_repair_invoker_fn_fails_job_and_logs_error(self, caplog, tmp_path):
        """When repair_invoker_fn is None, job registered then failed; ERROR logged.

        Verifies call order (register before fail) and job_id propagation.
        """
        mock_tracker = _make_job_tracker(active_jobs=[])
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=None,
        )

        with caplog.at_level(logging.ERROR):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_skipped_no_repair_invoker_fn" in caplog.text

        # register_job must have been called before fail_job
        call_names = [call[0] for call in mock_tracker.mock_calls]
        assert "register_job" in call_names
        assert "fail_job" in call_names
        assert call_names.index("register_job") < call_names.index("fail_job")

        # fail_job must have received the job_id from register_job
        registered_job_id = mock_tracker.register_job.return_value.job_id
        mock_tracker.fail_job.assert_called_once_with(
            registered_job_id, error=mock_tracker.fail_job.call_args[1]["error"]
        )


# ---------------------------------------------------------------------------
# Decision lock and in-flight guard
# ---------------------------------------------------------------------------


def _hold_auto_repair_lock(service: DependencyMapService) -> threading.Lock:
    """Pre-acquire the 'auto_repair' solo decision lock to simulate contention."""
    service._solo_decision_locks["auto_repair"] = threading.Lock()
    service._solo_decision_locks["auto_repair"].acquire()
    # cast is safe: we assigned a threading.Lock to this key two lines above;
    # _solo_decision_locks is typed Dict[str, Any] so the lookup returns Any.
    return cast(threading.Lock, service._solo_decision_locks["auto_repair"])


class TestDecisionLockAndInFlightGuard:
    """Decision lock contention and in-flight guard gate the auto_repair key."""

    def test_decision_lock_contended_no_repair(self, caplog, tmp_path):
        """When 'auto_repair' decision lock is held, logs skip and no repair fires."""
        mock_tracker = _make_job_tracker(active_jobs=[])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )
        held_lock = _hold_auto_repair_lock(service)

        try:
            with caplog.at_level(logging.INFO):
                service._maybe_run_auto_repair_after_scheduled("delta")
        finally:
            held_lock.release()

        assert "scheduled_auto_repair_skipped_decision_lock_held" in caplog.text
        mock_tracker.register_job.assert_not_called()
        repair_fn.assert_not_called()

    def test_in_flight_guard_triggers_no_repair(self, caplog, tmp_path):
        """When dep-map job is in-flight, logs reentrance skip and no repair fires."""
        active_job = TrackedJob(
            job_id="in-flight-id",
            operation_type="dependency_map_repair",
            status="running",
            username="system",
        )
        mock_tracker = _make_job_tracker(active_jobs=[active_job])
        repair_fn = MagicMock()
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))

        service = _make_service(
            tmp_path=tmp_path,
            dep_map_auto_repair_enabled=True,
            job_tracker=mock_tracker,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.INFO):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_skipped_reentrance" in caplog.text
        mock_tracker.register_job.assert_not_called()
        repair_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Anti-fallback guard: cluster mode without pg_pool (Story #927 Pass 2)
# ---------------------------------------------------------------------------


def _make_service_with_storage_mode(
    tmp_path,
    storage_mode: str,
    pg_pool=None,
    dep_map_auto_repair_enabled: bool = True,
    health_check_fn=None,
    repair_invoker_fn=None,
    job_tracker=None,
):
    """Create DependencyMapService with explicit storage_mode and pg_pool for guard tests."""
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_interval_hours = 24
    config.dep_map_auto_repair_enabled = dep_map_auto_repair_enabled
    config_manager = MagicMock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = MagicMock()
    golden_repos_manager.list_golden_repos.return_value = []
    golden_repos_manager.golden_repos_dir = str(tmp_path)

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=MagicMock(get_tracking=MagicMock(return_value={})),
        analyzer=MagicMock(),
        job_tracker=job_tracker or _make_job_tracker(active_jobs=[]),
        repair_invoker_fn=repair_invoker_fn,
        health_check_fn=health_check_fn,
        pg_pool=pg_pool,
        storage_mode=storage_mode,
    )


def _make_mock_pg_pool():
    """Build a mock pg_pool that simulates PG advisory lock acquisition (acquired=True).

    Returns (mock_pg_pool, mock_conn) so tests can assert on PG machinery calls.
    """
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (True,)

    mock_conn = MagicMock()
    mock_conn.execute.return_value = mock_cursor

    mock_txn_ctx = MagicMock()
    mock_txn_ctx.__enter__ = MagicMock(return_value=None)
    mock_txn_ctx.__exit__ = MagicMock(return_value=False)
    mock_conn.transaction.return_value = mock_txn_ctx

    mock_conn_ctx = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    mock_pg_pool = MagicMock()
    mock_pg_pool.connection.return_value = mock_conn_ctx

    return mock_pg_pool, mock_conn


class TestAntiFallbackClusterNoPgPool:
    """Story #927 Codex Pass 1 anti-fallback: cluster mode without pg_pool must refuse loudly.

    When storage_mode=postgres but pg_pool is None, the decision lock would silently
    degrade to a per-node threading.Lock, allowing duplicate auto-repair jobs across
    cluster nodes. The guard must log ERROR and return before attempting the lock.
    """

    def test_cluster_mode_no_pg_pool_logs_error(self, caplog, tmp_path):
        """When storage_mode=postgres and pg_pool=None, ERROR is logged with guard key."""
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))
        repair_fn = MagicMock()

        service = _make_service_with_storage_mode(
            tmp_path=tmp_path,
            storage_mode="postgres",
            pg_pool=None,
            dep_map_auto_repair_enabled=True,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.ERROR):
            service._maybe_run_auto_repair_after_scheduled("delta")

        assert "scheduled_auto_repair_misconfigured_cluster_no_pg_pool" in caplog.text

    def test_cluster_mode_no_pg_pool_repair_not_fired(self, tmp_path):
        """When storage_mode=postgres and pg_pool=None, repair_invoker_fn is never called."""
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))
        repair_fn = MagicMock()

        service = _make_service_with_storage_mode(
            tmp_path=tmp_path,
            storage_mode="postgres",
            pg_pool=None,
            dep_map_auto_repair_enabled=True,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        service._maybe_run_auto_repair_after_scheduled("delta")
        repair_fn.assert_not_called()

    def test_cluster_mode_with_pg_pool_passes_guard(self, caplog, tmp_path):
        """When storage_mode=postgres AND pg_pool is provided, guard does NOT block.

        A properly-configured cluster deployment must not be blocked by the guard.
        Verifies: (1) guard error key absent, (2) PG lock machinery exercised
        (connection/transaction/execute called), (3) repair fired.
        """
        health_fn = MagicMock(return_value=_make_health_report(anomaly_count=2))
        repair_fn = MagicMock()
        mock_pg_pool, mock_conn = _make_mock_pg_pool()

        service = _make_service_with_storage_mode(
            tmp_path=tmp_path,
            storage_mode="postgres",
            pg_pool=mock_pg_pool,
            dep_map_auto_repair_enabled=True,
            health_check_fn=health_fn,
            repair_invoker_fn=repair_fn,
        )

        with caplog.at_level(logging.ERROR):
            service._maybe_run_auto_repair_after_scheduled("delta")

        # Guard must not have fired
        assert (
            "scheduled_auto_repair_misconfigured_cluster_no_pg_pool" not in caplog.text
        )
        # PG lock machinery must have been exercised (cluster path, not solo path)
        mock_pg_pool.connection.assert_called_once()
        mock_conn.transaction.assert_called_once()
        mock_conn.execute.assert_called_once()
        # Repair must have been called (lock acquired, anomalies present)
        repair_fn.assert_called_once()
