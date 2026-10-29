"""/healthz serves the status a background refresher computed; the full health
aggregation never runs on the request path while the refresher is running.

Seams: the route module's ``health_service`` (a slow or blocking fake that
records the thread it runs on) and ``_healthz_monotonic`` (a controllable
clock shared by the request path and the refresher). See
test_healthz_liveness_endpoint_1433.py for the dual-import-path note behind
the module path below. The route answers ONLY ``{"status": ...}``, so
whole-body equality pins the liveness contract.
"""

import asyncio
import importlib
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict, Iterator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import create_app
from code_indexer.server.models.api_models import (
    HealthCheckResponse,
    HealthStatus,
    ServiceHealthInfo,
    SystemHealthInfo,
)

INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"
HEALTH_SERVICE_PATCH_TARGET = f"{INLINE_MISC_MODULE_PATH}.health_service"
CLOCK_PATCH_TARGET = f"{INLINE_MISC_MODULE_PATH}._healthz_monotonic"
SLOW_EVALUATION_SECONDS = 0.5
FAST_PROBE_SECONDS = SLOW_EVALUATION_SECONDS / 2
WAIT_BOUND_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.02
BEYOND_SYNC_TTL_SECONDS = 30.0
MARGIN_SECONDS = 1.0
SIMULATED_SLOW_EVALUATION_SECONDS = 250.0
EXPECTED_REFRESH_INTERVAL_SECONDS = 30.0
EXPECTED_STALENESS_BOUND_SECONDS = 120.0
PROBES = 5


def _response(status_value: HealthStatus) -> HealthCheckResponse:
    svc = ServiceHealthInfo(status=status_value, response_time_ms=1, error_message=None)
    return HealthCheckResponse(
        status=status_value,
        timestamp=datetime.now(timezone.utc),
        services={"database": svc, "storage": svc},
        system=SystemHealthInfo(
            memory_usage_percent=1.0,
            cpu_usage_percent=1.0,
            active_jobs=0,
            disk_free_space_gb=100.0,
            disk_read_kb_s=0.0,
            disk_write_kb_s=0.0,
            net_rx_kb_s=0.0,
            net_tx_kb_s=0.0,
        ),
        failure_reasons=[],
    )


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _SlowHealthService:
    def __init__(self, status_value: HealthStatus, fail: bool = False) -> None:
        self.status_value = status_value
        self.fail = fail
        self.threads: List[str] = []

    def get_system_health(self) -> HealthCheckResponse:
        self.threads.append(threading.current_thread().name)
        time.sleep(SLOW_EVALUATION_SECONDS)
        if self.fail:
            raise RuntimeError("simulated evaluation failure /secret/path")
        return _response(self.status_value)


class _ScriptedHealthService:
    """Each evaluation advances the fake clock, then blocks once the first
    `free_evaluations` are used up until `release` is set."""

    def __init__(self, clock: _Clock, advance: float, free_evaluations: int) -> None:
        self.clock = clock
        self.advance = advance
        self.free_evaluations = free_evaluations
        self.release = threading.Event()
        self.threads: List[str] = []
        self.completed = 0

    def get_system_health(self) -> HealthCheckResponse:
        self.threads.append(threading.current_thread().name)
        if len(self.threads) > self.free_evaluations:
            self.release.wait(WAIT_BOUND_SECONDS)
        self.clock.now += self.advance
        self.completed += 1
        return _response(HealthStatus.HEALTHY)


@pytest.fixture
def inline_misc() -> Iterator[ModuleType]:
    module = importlib.import_module(INLINE_MISC_MODULE_PATH)
    module._reset_healthz_cache()
    yield module
    module.stop_healthz_refresher()
    module._reset_healthz_cache()


def _wait_until(predicate: Callable[[], bool], failure_message: str) -> None:
    deadline = time.monotonic() + WAIT_BOUND_SECONDS
    while not predicate():
        assert time.monotonic() < deadline, failure_message
        time.sleep(POLL_INTERVAL_SECONDS)


def _start_and_wait(module: ModuleType) -> None:
    module.start_healthz_refresher()
    _wait_until(
        lambda: module._healthz_cached_status is not None,
        "refresher never evaluated",
    )


def _refresher_alive(module: ModuleType) -> bool:
    return any(
        t.name == module.HEALTHZ_REFRESHER_THREAD_NAME for t in threading.enumerate()
    )


def test_refresh_cadence_and_staleness_bound(inline_misc) -> None:
    assert inline_misc._HEALTHZ_REFRESH_INTERVAL_SECONDS == (
        EXPECTED_REFRESH_INTERVAL_SECONDS
    )
    assert inline_misc._HEALTHZ_MAX_AGE_SECONDS == EXPECTED_STALENESS_BOUND_SECONDS


def test_slow_aggregation_runs_only_on_refresher_thread(inline_misc) -> None:
    fake = _SlowHealthService(HealthStatus.DEGRADED)
    client = TestClient(create_app())
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake):
        _start_and_wait(inline_misc)
        durations = []
        for _ in range(PROBES):
            started = time.monotonic()
            response = client.get("/healthz")
            durations.append(time.monotonic() - started)
            assert response.status_code == 200
            assert response.json() == {"status": "degraded"}
            with patch(
                CLOCK_PATCH_TARGET,
                return_value=time.monotonic() + BEYOND_SYNC_TTL_SECONDS,
            ):
                assert client.get("/healthz").json() == {"status": "degraded"}

    assert set(fake.threads) == {inline_misc.HEALTHZ_REFRESHER_THREAD_NAME}
    assert max(durations) < FAST_PROBE_SECONDS


def test_status_is_stamped_when_the_evaluation_completes(inline_misc) -> None:
    clock = _Clock()
    fake = _ScriptedHealthService(
        clock, SIMULATED_SLOW_EVALUATION_SECONDS, free_evaluations=1
    )
    client = TestClient(create_app())
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake), patch(CLOCK_PATCH_TARGET, clock):
        _start_and_wait(inline_misc)
        completed_at = clock.now
        clock.now = completed_at + inline_misc._HEALTHZ_MAX_AGE_SECONDS - MARGIN_SECONDS
        response = client.get("/healthz")
        fake.release.set()

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_slow_in_flight_evaluation_is_unhealthy_only_past_the_bound(
    inline_misc,
) -> None:
    clock = _Clock()
    fake = _ScriptedHealthService(clock, advance=0.0, free_evaluations=1)
    client = TestClient(create_app())
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake), patch(CLOCK_PATCH_TARGET, clock):
        _start_and_wait(inline_misc)
        completed_at = clock.now
        clock.now = completed_at + 2 * inline_misc._HEALTHZ_REFRESH_INTERVAL_SECONDS
        within = client.get("/healthz")
        clock.now = completed_at + inline_misc._HEALTHZ_MAX_AGE_SECONDS + MARGIN_SECONDS
        past = client.get("/healthz")
        fake.release.set()

    assert (within.status_code, within.json()) == (200, {"status": "healthy"})
    assert (past.status_code, past.json()) == (503, {"status": "unhealthy"})
    assert set(fake.threads) == {inline_misc.HEALTHZ_REFRESHER_THREAD_NAME}


def test_hung_first_evaluation_is_healthy_until_the_bound_then_unhealthy(
    inline_misc,
) -> None:
    clock = _Clock()
    fake = _ScriptedHealthService(clock, advance=0.0, free_evaluations=0)
    client = TestClient(create_app())
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake), patch(CLOCK_PATCH_TARGET, clock):
        started_at = clock.now
        inline_misc.start_healthz_refresher()
        _wait_until(lambda: len(fake.threads) == 1, "refresher never started")
        early = client.get("/healthz")
        clock.now = started_at + inline_misc._HEALTHZ_MAX_AGE_SECONDS - MARGIN_SECONDS
        late_but_within = client.get("/healthz")
        clock.now = started_at + inline_misc._HEALTHZ_MAX_AGE_SECONDS + MARGIN_SECONDS
        past = client.get("/healthz")
        fake.release.set()

    assert (early.status_code, early.json()) == (200, {"status": "healthy"})
    assert late_but_within.status_code == 200
    assert (past.status_code, past.json()) == (503, {"status": "unhealthy"})
    assert set(fake.threads) == {inline_misc.HEALTHZ_REFRESHER_THREAD_NAME}


def test_failed_evaluation_is_served_as_unhealthy(inline_misc) -> None:
    fake = _SlowHealthService(HealthStatus.HEALTHY, fail=True)
    client = TestClient(create_app())
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake):
        _start_and_wait(inline_misc)
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}
    assert "secret" not in response.text
    assert set(fake.threads) == {inline_misc.HEALTHZ_REFRESHER_THREAD_NAME}


def test_start_is_idempotent_and_stop_ends_the_thread(inline_misc) -> None:
    fake = _SlowHealthService(HealthStatus.HEALTHY)
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake):
        inline_misc.start_healthz_refresher()
        inline_misc.start_healthz_refresher()
        names = [t.name for t in threading.enumerate()]
        assert names.count(inline_misc.HEALTHZ_REFRESHER_THREAD_NAME) == 1
        inline_misc.stop_healthz_refresher()
        _wait_until(lambda: not _refresher_alive(inline_misc), "refresher did not stop")


class _FailsAfterReleaseHealthService:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_system_health(self) -> HealthCheckResponse:
        self.entered.set()
        self.release.wait(WAIT_BOUND_SECONDS)
        raise RuntimeError("simulated failure during shutdown")


def test_evaluation_failing_after_stop_logs_no_error(
    inline_misc, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FailsAfterReleaseHealthService()
    caplog.set_level(logging.DEBUG, logger=INLINE_MISC_MODULE_PATH)
    with patch(HEALTH_SERVICE_PATCH_TARGET, fake):
        inline_misc.start_healthz_refresher()
        assert fake.entered.wait(WAIT_BOUND_SECONDS)
        inline_misc.stop_healthz_refresher()
        fake.release.set()
        _wait_until(lambda: not _refresher_alive(inline_misc), "refresher did not stop")

    errors = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "liveness evaluation" in r.getMessage()
    ]
    assert errors == []


def test_real_lifespan_runs_the_refresher_while_serving_and_stops_it(
    inline_misc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI

    from code_indexer.server.services.async_logging import IdentityQueueHandler
    from code_indexer.server.services.sqlite_log_handler import SQLiteLogHandler
    from code_indexer.server.startup.lifespan import make_lifespan
    from tests.unit.server.startup.test_lifespan_sqlite_handler_leak_bug1060 import (
        _make_minimal_lifespan_deps,
        _write_minimal_config,
    )

    data_dir = tmp_path / "server"
    data_dir.mkdir()
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(data_dir))
    _write_minimal_config(str(data_dir))
    app = FastAPI()
    lifespan_fn = make_lifespan(**_make_minimal_lifespan_deps())
    seen: Dict[str, bool] = {}

    async def _run() -> None:
        async with lifespan_fn(app):
            seen["running"] = inline_misc._healthz_refresher_stop is not None

    root = logging.getLogger()
    fake = _SlowHealthService(HealthStatus.HEALTHY)
    try:
        with patch(HEALTH_SERVICE_PATCH_TARGET, fake):
            asyncio.run(_run())
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, (SQLiteLogHandler, IdentityQueueHandler)):
                root.removeHandler(handler)

    assert seen.get("running") is True
    assert inline_misc._healthz_refresher_stop is None
    _wait_until(lambda: not _refresher_alive(inline_misc), "refresher did not stop")
