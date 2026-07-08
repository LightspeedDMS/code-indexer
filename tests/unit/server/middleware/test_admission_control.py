"""C7 admission control: shed excess in-flight requests with 429 + Retry-After."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from code_indexer.server.middleware.admission_control import (
    AdmissionControlMiddleware,
    AdmissionController,
    PerConsumerRateLimiter,
    _ANON_CONSUMER,
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


# ---------------------------------------------------------------------------
# Per-consumer rate limiting (dealer-wide fairness)
# ---------------------------------------------------------------------------


def _rl_client(rate_limiter=None, controller=None):
    async def ok(request):
        return PlainTextResponse("ok")

    async def health(request):
        return PlainTextResponse("healthy")

    app = Starlette(routes=[Route("/api/x", ok), Route("/health", health)])
    app.add_middleware(
        AdmissionControlMiddleware,
        controller=controller,
        rate_limiter=rate_limiter,
    )
    return TestClient(app)


def _bare_request(headers=None, cookies=None) -> Request:
    raw = []
    for k, v in (headers or {}).items():
        raw.append((k.lower().encode(), v.encode()))
    if cookies:
        cookie_val = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw.append((b"cookie", cookie_val.encode()))
    return Request({"type": "http", "headers": raw})


def test_per_consumer_sheds_after_burst_and_is_isolated():
    # refill ~0 during the test so capacity is the exact burst allowance.
    rl = PerConsumerRateLimiter(capacity=2, refill_per_second=0.001)
    client = _rl_client(rate_limiter=rl)
    ha = {"Authorization": "Bearer aaa"}
    hb = {"Authorization": "Bearer bbb"}

    assert client.get("/api/x", headers=ha).status_code == 200
    assert client.get("/api/x", headers=ha).status_code == 200
    shed = client.get("/api/x", headers=ha)
    assert shed.status_code == 429
    assert int(shed.headers["Retry-After"]) >= 1
    assert shed.json()["retry_after_seconds"] >= 1
    # A different consumer has its own bucket and is unaffected.
    assert client.get("/api/x", headers=hb).status_code == 200


def test_per_consumer_exempts_health_even_when_throttled():
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.001)
    client = _rl_client(rate_limiter=rl)
    h = {"Authorization": "Bearer x"}
    assert client.get("/api/x", headers=h).status_code == 200  # burn the token
    assert client.get("/api/x", headers=h).status_code == 429
    # readiness probe must never be rate-limited
    assert client.get("/health", headers=h).status_code == 200


def test_anonymous_requests_share_one_bucket():
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.001)
    client = _rl_client(rate_limiter=rl)
    assert client.get("/api/x").status_code == 200  # anon token 1
    assert client.get("/api/x").status_code == 429  # anon bucket empty


def test_rate_limit_checked_before_global_cap_slot():
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.001)
    ctrl = AdmissionController(max_inflight=5, retry_after_seconds=2)
    client = _rl_client(rate_limiter=rl, controller=ctrl)
    h = {"Authorization": "Bearer q"}

    assert client.get("/api/x", headers=h).status_code == 200
    assert client.get("/api/x", headers=h).status_code == 429  # rate-limited
    # The shed request must NOT have consumed (or leaked) a global in-flight slot.
    assert ctrl.inflight == 0


def test_consumer_key_prefers_auth_then_cookie_then_anon():
    k_auth = PerConsumerRateLimiter.consumer_key(
        _bare_request(headers={"Authorization": "Bearer z"})
    )
    k_auth_same = PerConsumerRateLimiter.consumer_key(
        _bare_request(headers={"Authorization": "Bearer z"})
    )
    k_auth_diff = PerConsumerRateLimiter.consumer_key(
        _bare_request(headers={"Authorization": "Bearer y"})
    )
    assert k_auth == k_auth_same, "same credential -> same bucket key"
    assert k_auth != k_auth_diff, "different credential -> different bucket key"
    assert k_auth != _ANON_CONSUMER
    assert "Bearer" not in k_auth, "raw credential must not appear in the key"

    k_cookie = PerConsumerRateLimiter.consumer_key(
        _bare_request(cookies={"cidx_session": "tok"})
    )
    assert k_cookie not in (_ANON_CONSUMER, k_auth)

    assert PerConsumerRateLimiter.consumer_key(_bare_request()) == _ANON_CONSUMER
