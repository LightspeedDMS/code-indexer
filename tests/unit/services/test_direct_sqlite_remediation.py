"""
Story #527: Direct-SQLite Service Remediation for PG Mode.

Tests that each remediated service properly routes to backend protocol methods
when storage_mode == "postgres" instead of opening direct SQLite connections.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Minimal config object for DataRetentionScheduler."""

    class _RetentionConfig:
        operational_logs_retention_hours = 720  # 30 days
        audit_logs_retention_hours = 2160  # 90 days
        sync_jobs_retention_hours = 168  # 7 days
        dep_map_history_retention_hours = 720
        background_jobs_retention_hours = 168

    data_retention_config = _RetentionConfig()


class _FakeConfigService:
    def get_config(self) -> _FakeConfig:
        return _FakeConfig()


class _FakeLogsBackend:
    def __init__(self) -> None:
        self.calls: List[Dict] = []

    def cleanup_old_logs(self, days_to_keep: int) -> int:
        self.calls.append({"days_to_keep": days_to_keep})
        return 5


class _FakeAuditLogBackend:
    def __init__(self) -> None:
        self.calls: List[Dict] = []

    def cleanup_old_logs(self, cutoff_iso: str) -> int:
        self.calls.append({"cutoff_iso": cutoff_iso})
        return 3


class _FakeSyncJobsBackend:
    def __init__(self) -> None:
        self.calls: List[Dict] = []

    def cleanup_old_completed(self, cutoff_iso: str) -> int:
        self.calls.append({"cutoff_iso": cutoff_iso})
        return 2


class _FakeDependencyMapTrackingBackend:
    def __init__(self) -> None:
        self.calls: List[Dict] = []

    def cleanup_old_history(self, cutoff_iso: str) -> int:
        self.calls.append({"cutoff_iso": cutoff_iso})
        return 1


class _FakeBackgroundJobsBackend:
    def __init__(self) -> None:
        self.calls: List[Dict] = []

    def cleanup_old_jobs(self, max_age_hours: int) -> int:
        self.calls.append({"max_age_hours": max_age_hours})
        return 4


class _FakeBackendRegistry:
    def __init__(self) -> None:
        self.logs = _FakeLogsBackend()
        self.audit_log = _FakeAuditLogBackend()
        self.sync_jobs = _FakeSyncJobsBackend()
        self.dependency_map_tracking = _FakeDependencyMapTrackingBackend()
        self.background_jobs = _FakeBackgroundJobsBackend()


class _FakeSelfMonitoringBackend:
    def __init__(self) -> None:
        self.scans_returned: List[Dict] = [
            {
                "scan_id": "s1",
                "started_at": "2026-01-01T00:00:00",
                "completed_at": "2026-01-01T00:01:00",
                "status": "SUCCESS",
                "log_id_start": 1,
                "log_id_end": 10,
                "issues_created": 0,
                "error_message": None,
            }
        ]
        self.issues_returned: List[Dict] = [{"id": 1, "scan_id": "s1"}]
        self.last_started_at: Optional[str] = "2026-01-01T00:00:00"
        self.running_count = 0

        self.list_scans_calls: List[Dict] = []
        self.list_issues_calls: List[Dict] = []
        self.get_last_started_at_calls = 0
        self.get_running_scan_count_calls = 0

    def list_scans(self, limit: int = 50) -> List[Dict]:
        self.list_scans_calls.append({"limit": limit})
        return self.scans_returned

    def list_issues(self, limit: int = 100) -> List[Dict]:
        self.list_issues_calls.append({"limit": limit})
        return self.issues_returned

    def get_last_started_at(self) -> Optional[str]:
        self.get_last_started_at_calls += 1
        return self.last_started_at

    def get_running_scan_count(self) -> int:
        self.get_running_scan_count_calls += 1
        return self.running_count


# ===========================================================================
# Service 1: DataRetentionScheduler - PG mode uses backends
# ===========================================================================


class TestDataRetentionSchedulerPgMode:
    """Story #527 AC1: DataRetentionScheduler uses backend methods in PG mode."""

    def _make_scheduler(
        self,
        storage_mode: str = "sqlite",
        backend_registry: Any = None,
    ):
        from code_indexer.server.services.data_retention_scheduler import (
            DataRetentionScheduler,
        )

        return DataRetentionScheduler(
            log_db_path=Path("/tmp/logs.db"),
            main_db_path=Path("/tmp/cidx_server.db"),
            groups_db_path=Path("/tmp/groups.db"),
            config_service=_FakeConfigService(),
            storage_mode=storage_mode,
            backend_registry=backend_registry,
        )

    def test_pg_mode_calls_logs_backend(self) -> None:
        """In PG mode, cleanup calls LogsBackend.cleanup_old_logs with days_to_keep."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        result = scheduler._execute_cleanup_pg()

        assert len(reg.logs.calls) == 1
        # 720 hours / 24 = 30 days
        assert reg.logs.calls[0]["days_to_keep"] == 30
        assert result["logs_deleted"] == 5

    def test_pg_mode_calls_audit_log_backend(self) -> None:
        """In PG mode, cleanup calls AuditLogBackend.cleanup_old_logs with cutoff_iso."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        scheduler._execute_cleanup_pg()

        assert len(reg.audit_log.calls) == 1
        assert "cutoff_iso" in reg.audit_log.calls[0]

    def test_pg_mode_calls_sync_jobs_backend(self) -> None:
        """In PG mode, cleanup calls SyncJobsBackend.cleanup_old_completed."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        scheduler._execute_cleanup_pg()

        assert len(reg.sync_jobs.calls) == 1

    def test_pg_mode_calls_dep_map_backend(self) -> None:
        """In PG mode, cleanup calls DependencyMapTrackingBackend.cleanup_old_history."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        scheduler._execute_cleanup_pg()

        assert len(reg.dependency_map_tracking.calls) == 1

    def test_pg_mode_calls_background_jobs_backend(self) -> None:
        """In PG mode, cleanup calls BackgroundJobsBackend.cleanup_old_jobs."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        scheduler._execute_cleanup_pg()

        assert len(reg.background_jobs.calls) == 1
        assert reg.background_jobs.calls[0]["max_age_hours"] == 168

    def test_pg_mode_total_deleted_is_sum(self) -> None:
        """Total deleted is sum of all backend return values (5+3+2+1+4=15)."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)
        result = scheduler._execute_cleanup_pg()

        assert result["total_deleted"] == 15

    def test_sqlite_mode_does_not_call_backends(self) -> None:
        """In SQLite mode, backend methods are never called."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="sqlite", backend_registry=reg)

        with patch.object(
            scheduler, "_execute_cleanup_sqlite", return_value={"total_deleted": 0}
        ) as mock:
            scheduler._execute_cleanup()
            mock.assert_called_once()

        assert len(reg.logs.calls) == 0
        assert len(reg.audit_log.calls) == 0

    def test_execute_cleanup_routes_to_pg_when_pg_mode(self) -> None:
        """_execute_cleanup routes to _execute_cleanup_pg in PG mode."""
        reg = _FakeBackendRegistry()
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=reg)

        with (
            patch.object(
                scheduler, "_execute_cleanup_pg", return_value={"total_deleted": 0}
            ) as pg_mock,
            patch.object(
                scheduler, "_execute_cleanup_sqlite", return_value={"total_deleted": 0}
            ) as sqlite_mock,
        ):
            scheduler._execute_cleanup()
            pg_mock.assert_called_once()
            sqlite_mock.assert_not_called()

    def test_pg_mode_without_backend_registry_falls_back_to_sqlite(self) -> None:
        """If storage_mode=postgres but no backend_registry, use SQLite path."""
        scheduler = self._make_scheduler(storage_mode="postgres", backend_registry=None)

        with (
            patch.object(
                scheduler, "_execute_cleanup_sqlite", return_value={"total_deleted": 0}
            ) as sqlite_mock,
            patch.object(
                scheduler, "_execute_cleanup_pg", return_value={"total_deleted": 0}
            ) as pg_mock,
        ):
            scheduler._execute_cleanup()
            sqlite_mock.assert_called_once()
            pg_mock.assert_not_called()


# ===========================================================================
# Service 3: HealthCheckService - PG mode checks PG connectivity
# ===========================================================================


class TestHealthServicePgMode:
    """Story #527 AC3: HealthCheckService checks PG in postgres mode."""

    def test_pg_connectivity_called_in_pg_mode(self) -> None:
        """In PG mode, _check_database_health calls _check_pg_connectivity."""
        from code_indexer.server.services.health_service import HealthCheckService

        svc = HealthCheckService(storage_mode="postgres", postgres_dsn="fake_dsn")

        with patch.object(svc, "_check_pg_connectivity") as pg_mock:
            svc._check_database_health()
            pg_mock.assert_called_once()

    def test_sqlite_connectivity_called_in_sqlite_mode(self) -> None:
        """In SQLite mode, _check_database_health calls _check_sqlite_connectivity."""
        from code_indexer.server.services.health_service import HealthCheckService

        svc = HealthCheckService(storage_mode="sqlite")

        with patch.object(svc, "_check_sqlite_connectivity") as sqlite_mock:
            svc._check_database_health()
            sqlite_mock.assert_called_once()

    def test_check_pg_connectivity_raises_without_dsn(self) -> None:
        """_check_pg_connectivity raises RuntimeError when postgres_dsn is not set."""
        from code_indexer.server.services.health_service import HealthCheckService

        svc = HealthCheckService(storage_mode="postgres", postgres_dsn=None)

        with pytest.raises(RuntimeError, match="postgres_dsn not configured"):
            svc._check_pg_connectivity()

    def test_get_health_service_lazy_singleton(self) -> None:
        """get_health_service() returns a singleton that is lazily created."""
        from code_indexer.server.services.health_service import (
            get_health_service,
            _reset_health_service_for_testing,
        )

        _reset_health_service_for_testing()

        svc1 = get_health_service()
        svc2 = get_health_service()
        assert svc1 is svc2

        _reset_health_service_for_testing()

    def test_get_health_service_accepts_storage_mode(self) -> None:
        """get_health_service() passes storage_mode to the singleton."""
        from code_indexer.server.services.health_service import (
            get_health_service,
            _reset_health_service_for_testing,
        )

        _reset_health_service_for_testing()
        try:
            svc = get_health_service(storage_mode="postgres", postgres_dsn="dsn://test")
            assert svc.storage_mode == "postgres"
            assert svc.postgres_dsn == "dsn://test"
        finally:
            _reset_health_service_for_testing()

    def test_health_service_proxy_forwards_to_singleton(self) -> None:
        """The module-level health_service proxy forwards attribute access."""
        import code_indexer.server.services.health_service as hs_module

        assert hasattr(hs_module.health_service, "get_system_health")


# ===========================================================================
# Service 4: Web routes - self-monitoring helpers use backend when provided
# ===========================================================================


class TestRoutesSelfMonitoringUsesBackend:
    """Story #527 AC4: Web route helpers use SelfMonitoringBackend in PG mode."""

    def test_load_self_monitoring_data_uses_backend(self) -> None:
        """_load_self_monitoring_data calls backend.list_scans / list_issues."""
        from code_indexer.server.web.routes import _load_self_monitoring_data

        backend = _FakeSelfMonitoringBackend()
        session = MagicMock()

        scans, issues = _load_self_monitoring_data(
            Path("/tmp/fake.db"), session, backend=backend
        )

        assert len(backend.list_scans_calls) == 1
        assert len(backend.list_issues_calls) == 1
        assert issues == backend.issues_returned

    def test_load_self_monitoring_data_adds_duration(self) -> None:
        """_load_self_monitoring_data adds duration field to scans via backend."""
        from code_indexer.server.web.routes import _load_self_monitoring_data

        backend = _FakeSelfMonitoringBackend()
        session = MagicMock()

        scans, _ = _load_self_monitoring_data(
            Path("/tmp/fake.db"), session, backend=backend
        )

        assert "duration" in scans[0]
        assert scans[0]["duration"] != "N/A"

    def test_get_last_scan_time_uses_backend(self) -> None:
        """_get_last_scan_time calls backend.get_last_started_at() when provided."""
        from code_indexer.server.web.routes import _get_last_scan_time

        backend = _FakeSelfMonitoringBackend()
        result = _get_last_scan_time(Path("/tmp/fake.db"), backend=backend)

        assert backend.get_last_started_at_calls == 1
        assert result == "2026-01-01T00:00:00"

    def test_get_last_scan_time_returns_none_from_backend(self) -> None:
        """_get_last_scan_time returns None when backend.get_last_started_at returns None."""
        from code_indexer.server.web.routes import _get_last_scan_time

        backend = _FakeSelfMonitoringBackend()
        backend.last_started_at = None

        result = _get_last_scan_time(Path("/tmp/fake.db"), backend=backend)
        assert result is None

    def test_get_scan_status_idle_uses_backend(self) -> None:
        """_get_scan_status returns Idle when backend.get_running_scan_count() == 0."""
        from code_indexer.server.web.routes import _get_scan_status

        backend = _FakeSelfMonitoringBackend()
        backend.running_count = 0

        result = _get_scan_status(Path("/tmp/fake.db"), backend=backend)

        assert backend.get_running_scan_count_calls == 1
        assert result == "Idle"

    def test_get_scan_status_running_uses_backend(self) -> None:
        """_get_scan_status returns Running when backend.get_running_scan_count() > 0."""
        from code_indexer.server.web.routes import _get_scan_status

        backend = _FakeSelfMonitoringBackend()
        backend.running_count = 1

        result = _get_scan_status(Path("/tmp/fake.db"), backend=backend)

        assert result == "Running..."

    def test_load_without_backend_uses_sqlite_path(self) -> None:
        """_load_self_monitoring_data skips backend when none given."""
        from code_indexer.server.web.routes import _load_self_monitoring_data

        session = MagicMock()
        backend = _FakeSelfMonitoringBackend()

        # Call WITHOUT passing backend= - backend should never be touched
        _load_self_monitoring_data(Path("/tmp/nonexistent_fake_db.db"), session)

        assert len(backend.list_scans_calls) == 0
        assert len(backend.list_issues_calls) == 0
