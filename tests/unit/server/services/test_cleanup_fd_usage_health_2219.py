"""Bug #2219: high descriptor usage seen by the cleanup manager is DEGRADED on health."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo

SOFT_LIMIT = 1000


def _cleanup_manager(used: List[int]) -> CleanupManager:
    manager = CleanupManager(query_tracker=QueryTracker(), min_retention_age_seconds=0)
    manager._measure_fd_usage = lambda: (used[0], SOFT_LIMIT)  # type: ignore[method-assign]
    manager._process_cleanup_queue()
    return manager


def _service() -> Any:
    from code_indexer.server.services.health_service import HealthCheckService

    return HealthCheckService.__new__(HealthCheckService)


def _wire(monkeypatch: pytest.MonkeyPatch, lifecycle: Any) -> None:
    from code_indexer.server.app import app as app_module

    monkeypatch.setattr(
        app_module.state, "global_lifecycle_manager", lifecycle, raising=False
    )


def test_collector_is_degraded_only_while_usage_is_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, SimpleNamespace(cleanup_manager=_cleanup_manager([900])))

    warn, err, reasons = _service()._collect_cleanup_fd_usage_failures()

    assert (warn, err, len(reasons)) == (True, False, 1)
    assert "900" in reasons[0] and str(SOFT_LIMIT) in reasons[0]


def test_collector_reports_nothing_when_usage_is_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, SimpleNamespace(cleanup_manager=_cleanup_manager([10])))

    assert _service()._collect_cleanup_fd_usage_failures() == (False, False, [])


def test_collector_reports_nothing_without_lifecycle_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, None)

    assert _service()._collect_cleanup_fd_usage_failures() == (False, False, [])


def test_overall_status_is_degraded_with_the_fd_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, SimpleNamespace(cleanup_manager=_cleanup_manager([900])))
    service = _service()
    monkeypatch.setattr(
        service,
        "_collect_golden_repos_storage_failures",
        lambda: (False, False, []),
    )
    system = SystemHealthInfo(
        memory_usage_percent=10.0,
        cpu_usage_percent=10.0,
        active_jobs=0,
        disk_free_space_gb=100.0,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
    )
    service._cpu_history = []
    import threading

    service._cpu_history_lock = threading.Lock()

    status, reasons = service._calculate_overall_status({}, system, [])

    assert status == HealthStatus.DEGRADED
    assert any("File descriptor usage" in r for r in reasons)
