"""C7 admission control: shed excess in-flight requests with 429 + Retry-After."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from code_indexer.server.middleware.admission_control import (
    AdmissionControlMiddleware,
    AdmissionController,
)


def _client(controller):
    async def ok(request):
        return PlainTextResponse("ok")

    async def health(request):
        return PlainTextResponse("healthy")

    app = Starlette(
        routes=[Route("/api/x", ok), Route("/health", health), Route("/docs", ok)]
    )
    app.add_middleware(AdmissionControlMiddleware, controller=controller)
    return TestClient(app)


def test_admits_and_releases_when_under_cap():
    ctrl = AdmissionController(max_inflight=2, retry_after_seconds=1)
    client = _client(ctrl)
    assert client.get("/api/x").status_code == 200
    assert ctrl.inflight == 0  # slot released after the request completes


def test_sheds_with_429_and_retry_after_when_full():
    ctrl = AdmissionController(max_inflight=1, retry_after_seconds=3)
    ctrl.try_enter()  # occupy the only slot (simulate an in-flight request)
    resp = _client(ctrl).get("/api/x")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3"
    assert resp.json()["retry_after_seconds"] == 3


def test_exempt_paths_never_shed_even_when_full():
    ctrl = AdmissionController(max_inflight=1, retry_after_seconds=1)
    ctrl.try_enter()  # full
    client = _client(ctrl)
    # readiness probe + schema must still succeed under overload
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200


def test_zero_cap_means_no_limit():
    ctrl = AdmissionController(max_inflight=0, retry_after_seconds=1)
    assert all(ctrl.try_enter() for _ in range(10))


def test_controller_counts_and_releases():
    ctrl = AdmissionController(max_inflight=2, retry_after_seconds=1)
    assert ctrl.try_enter() and ctrl.try_enter()  # 2/2
    assert ctrl.try_enter() is False  # full
    ctrl.leave()
    assert ctrl.inflight == 1
    assert ctrl.try_enter() is True  # room again
