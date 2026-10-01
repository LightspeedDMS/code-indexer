"""Lifespan wiring: the offloaded, awaited registration barrier."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, List

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services import config_service as config_service_mod
from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.siem_delivery import capture, lifecycle, state_store
from code_indexer.server.services.siem_delivery.db import SiemDb, SiemTx
from code_indexer.server.storage.database_manager import DatabaseSchema

INJECTED_DELAY = 2.0
MAX_LOOP_LAG = 0.1


class _SlowRegistrationDb(SiemDb):
    """The real SQLite store; the registration write records its thread and
    is delayed (a slow database) so a blocked event loop would show."""

    def __init__(self, path: str) -> None:
        real = SiemDb.sqlite(path)
        super().__init__(
            real.dialect, conn_manager=real._conn_manager, groups_db_path=path
        )
        self.write_threads: List[int] = []

    def write(self, fn: Callable[[SiemTx], Any], *, phase: str = "write") -> Any:
        if phase == "process_status" and not self.write_threads:
            self.write_threads.append(threading.get_ident())
            time.sleep(INJECTED_DELAY)  # test-only slow database
        return super().write(fn, phase=phase)


@pytest.fixture()
def wired(tmp_path: Path) -> Iterator[SimpleNamespace]:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    db_path = server_dir / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    svc = config_service_mod.ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    svc.initialize_runtime_db(str(db_path))
    config_service_mod.set_config_service(svc)
    groups = tmp_path / "groups.db"
    AuditLogService(groups)
    app = SimpleNamespace(
        state=SimpleNamespace(
            http_client_factory=HttpClientFactory(fault_injection_service=None),
            fault_injection_service=None,
        )
    )
    registry = SimpleNamespace(siem_delivery=_SlowRegistrationDb(str(groups)))
    try:
        yield SimpleNamespace(app=app, registry=registry)
    finally:
        scheduler = getattr(app.state, "siem_delivery_scheduler", None)
        if scheduler is not None:
            scheduler.stop()
        config_service_mod.reset_config_service()
        capture.reset_capture_state_for_tests()


def test_registration_is_offloaded_awaited_and_keeps_the_loop_responsive(
    wired: SimpleNamespace,
) -> None:
    async def _scenario() -> Any:
        loop_thread = threading.get_ident()
        lags: List[float] = []
        done = asyncio.Event()

        async def _probe() -> None:
            last = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                lags.append(now - last - 0.01)
                last = now

        probe = asyncio.create_task(_probe())
        scheduler = await lifecycle.start_scheduler(wired.app, wired.registry, None)
        done.set()
        await probe
        return loop_thread, lags, scheduler

    loop_thread, lags, scheduler = asyncio.run(_scenario())
    assert scheduler is not None
    db = wired.registry.siem_delivery
    assert db.write_threads and db.write_threads[0] != loop_thread
    assert max(lags) < MAX_LOOP_LAG, max(lags)
    live = state_store.live_processes(db)
    assert scheduler.process_id in [p["process_id"] for p in live]
    assert wired.app.state.siem_delivery_startup_error is None


def test_startup_failure_degrades_and_is_recorded(wired: SimpleNamespace) -> None:
    scheduler = asyncio.run(lifecycle.start_scheduler(wired.app, None, None))
    assert scheduler is None
    assert wired.app.state.siem_delivery_scheduler is None
    assert wired.app.state.siem_delivery_startup_error == "RuntimeError"


def test_health_collection_failure_is_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A SIEM health collection that raises is logged at WARNING (it stays
    fail-open: no reason, no error), never swallowed at DEBUG."""
    import logging

    from code_indexer.server import app as app_mod
    from code_indexer.server.services.health_service import HealthCheckService

    class _Broken:
        def health_inputs(self) -> Any:
            raise RuntimeError("collector-broke")

    state = app_mod.app.state
    monkeypatch.setattr(state, "siem_delivery_startup_error", None, raising=False)
    monkeypatch.setattr(state, "siem_delivery_scheduler", _Broken(), raising=False)
    with caplog.at_level(logging.WARNING):
        result = HealthCheckService()._collect_siem_delivery_failures()
    assert result == (False, False, [])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("collector-broke" in r.getMessage() for r in warnings)
