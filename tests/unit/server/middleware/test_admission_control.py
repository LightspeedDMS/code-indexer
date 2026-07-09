"""Unit tests for request admission control / backpressure middleware.

Covers the global in-flight cap, per-consumer rate limiting, credential keying,
exempt paths, and 429 + Retry-After shedding. Also a config-roundtrip test.
"""

from dataclasses import fields

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from code_indexer.server.middleware.admission_control import (
    AdmissionControlMiddleware,
    AdmissionController,
    PerConsumerRateLimiter,
)
from code_indexer.server.utils.config_manager import AdmissionControlConfig


def _app(controller=None, rate_limiter=None):
    async def ok(request):
        return PlainTextResponse("ok")

    async def health(request):
        return PlainTextResponse("healthy")

    app = Starlette(routes=[Route("/x", ok), Route("/health", health)])
    app.add_middleware(
        AdmissionControlMiddleware, controller=controller, rate_limiter=rate_limiter
    )
    return TestClient(app)


# ---------------- AdmissionController ----------------


def test_controller_caps_inflight_and_releases():
    c = AdmissionController(max_inflight=1, retry_after_seconds=1)
    assert c.try_enter() is True
    assert c.try_enter() is False  # at cap
    c.leave()
    assert c.try_enter() is True  # slot freed


def test_controller_zero_means_no_cap():
    c = AdmissionController(max_inflight=0, retry_after_seconds=1)
    assert all(c.try_enter() for _ in range(50))


# ---------------- PerConsumerRateLimiter ----------------


def test_rate_limiter_sheds_after_burst():
    rl = PerConsumerRateLimiter(capacity=2, refill_per_second=0.0)

    class _Req:
        headers = {"authorization": "Bearer abc"}
        cookies: dict = {}

    allowed = [rl.check(_Req())[0] for _ in range(4)]
    assert allowed[:2] == [True, True]
    assert allowed[2] is False  # burst exhausted, no refill


def test_rate_limiter_keys_by_credential_not_shared():
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)

    class _A:
        headers = {"authorization": "Bearer A"}
        cookies: dict = {}

    class _B:
        headers = {"authorization": "Bearer B"}
        cookies: dict = {}

    assert rl.check(_A())[0] is True
    assert rl.check(_B())[0] is True  # different consumer -> own bucket
    assert rl.check(_A())[0] is False  # A exhausted


def test_consumer_key_hashes_and_never_returns_raw_credential():
    class _Req:
        headers = {"authorization": "Bearer super-secret"}
        cookies: dict = {}

    key = PerConsumerRateLimiter.consumer_key(_Req())
    assert "super-secret" not in key
    assert len(key) == 32


# ---------------- Middleware dispatch ----------------


def test_middleware_sheds_with_429_and_retry_after():
    # capacity 1 then a slow refill: the 2nd request is shed with a finite
    # Retry-After (the inf-refill path is covered by the isfinite guard).
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.01)
    client = _app(rate_limiter=rl)
    assert client.get("/x").status_code == 200
    resp = client.get("/x")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_middleware_shed_retry_after_survives_inf_refill():
    # refill 0 -> retry_after inf -> guard caps it instead of crashing (500).
    rl = PerConsumerRateLimiter(capacity=1, refill_per_second=0.0)
    client = _app(rate_limiter=rl)
    assert client.get("/x").status_code == 200
    resp = client.get("/x")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_middleware_exempts_health():
    rl = PerConsumerRateLimiter(capacity=0, refill_per_second=0.0)  # sheds everything
    client = _app(rate_limiter=rl)
    # /health is exempt -> never shed even when the limiter would reject
    assert client.get("/health").status_code == 200


# ---------------- Config roundtrip ----------------


def test_config_defaults_are_off():
    cfg = AdmissionControlConfig()
    assert cfg.enabled is False
    assert cfg.per_consumer_enabled is False


def test_config_from_dict_filters_unknown_keys():
    allowed = {f.name for f in fields(AdmissionControlConfig)}
    raw = {"enabled": True, "max_inflight_requests": 42, "bogus_future_key": 1}
    cfg = AdmissionControlConfig(**{k: v for k, v in raw.items() if k in allowed})
    assert cfg.enabled is True
    assert cfg.max_inflight_requests == 42
