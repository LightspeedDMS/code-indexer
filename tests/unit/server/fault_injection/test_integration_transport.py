"""
Integration tests for FaultInjectingTransport with a real httpx.AsyncClient.

Story #746 — M5 integration test layer.

Design:
  - The "real backend" is a minimal ASGI echo application served via
    httpx.ASGITransport.  This avoids real network calls while exercising
    the complete httpx request/response pipeline.
  - FaultInjectingTransport wraps the ASGITransport so that each fault mode
    is applied on top of real httpx infrastructure.
  - All 13 fault-mode outcomes are tested end-to-end through a real
    httpx.AsyncClient with real httpx transport objects (no mocking).
  - The latency test uses the default asyncio.sleep path and measures real
    elapsed time to verify the delay was applied.

ASGI echo app contract:
  - POST /echo → 200 with body {"echo": "ok"}
  - Any other path → 404

Fault modes covered (one test per mode):
  1.  pass_through        — no fault profile, request reaches echo app
  2.  http_error          — synthetic non-2xx response, no backend call
  3.  connect_timeout     — raises httpx.ConnectTimeout
  4.  read_timeout        — raises httpx.ReadTimeout
  5.  write_timeout       — raises httpx.WriteTimeout
  6.  pool_timeout        — raises httpx.PoolTimeout
  7.  connect_error       — raises httpx.ConnectError
  8.  dns_failure         — raises httpx.ConnectError (from socket.gaierror)
  9.  tls_error           — raises httpx.ConnectError (from ssl.SSLError)
  10. malformed_json       — 200 with corrupted body
  11. redirect_loop        — 302 self-referential Location header
  12. stream_disconnect    — 200 then httpx.StreamError mid-stream
  13. latency             — additive delay measured via real asyncio.sleep
"""

from __future__ import annotations

import json
import random
import time

import httpx
import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_injecting_transport import (
    FaultInjectingTransport,
)
from code_indexer.server.fault_injection.fault_profile import FaultProfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEED = 42
_TARGET = "echo.integration.test"
_BASE_URL = f"https://{_TARGET}"
_ECHO_PATH = "/echo"

_ERROR_CODE_502 = 502
_RETRY_AFTER_MIN = 1
_RETRY_AFTER_MAX = 3
_TRUNCATE_BYTES = 30
_LARGE_BODY = b"Z" * 200

# Latency constant used for the real-sleep test.
# Small enough to keep the test fast; large enough to measure reliably.
_LATENCY_CONSTANT_MS = 50
_LATENCY_TOLERANCE_FACTOR = 0.5  # allow 50% under-measurement for CI jitter

_HTTP_STATUS_OK = 200
_HTTP_STATUS_REDIRECT = 302
_HTTP_STATUS_NOT_FOUND = 404


# ---------------------------------------------------------------------------
# ASGI echo application
# ---------------------------------------------------------------------------


async def _echo_app(scope, receive, send) -> None:
    """Minimal ASGI app: POST /echo -> 200 {"echo": "ok"}, otherwise 404."""
    assert scope["type"] == "http"
    path = scope.get("path", "/")
    if path == _ECHO_PATH:
        status = _HTTP_STATUS_OK
        body = b'{"echo": "ok"}'
    else:
        status = _HTTP_STATUS_NOT_FOUND
        body = b'{"error": "not_found"}'

    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _large_echo_app(scope, receive, send) -> None:
    """ASGI app that returns a large body, used for stream_disconnect testing."""
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": _HTTP_STATUS_OK,
            "headers": [[b"content-type", b"application/octet-stream"]],
        }
    )
    await send({"type": "http.response.body", "body": _LARGE_BODY})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(seed: int = _SEED) -> FaultInjectionService:
    return FaultInjectionService(enabled=True, rng=random.Random(seed))


def _make_profile(**kwargs) -> FaultProfile:
    """Build a FaultProfile with minimum required defaults."""
    base: dict = {"target": _TARGET, "error_rate": 0.0, "error_codes": []}
    base.update(kwargs)
    if base.get("error_rate", 0.0) > 0.0 and not base.get("error_codes"):
        base["error_codes"] = [_ERROR_CODE_502]
    if base.get("malformed_rate", 0.0) > 0.0 and not base.get("corruption_modes"):
        base["corruption_modes"] = ["truncate"]
    return FaultProfile(**base)


def _make_client(
    svc: FaultInjectionService,
    asgi_app=_echo_app,
) -> httpx.AsyncClient:
    """Return an AsyncClient whose transport chain is:
    FaultInjectingTransport → ASGITransport(asgi_app).
    """
    fault_transport = FaultInjectingTransport(
        wrapped=httpx.ASGITransport(app=asgi_app),
        service=svc,
    )
    return httpx.AsyncClient(
        transport=fault_transport,
        base_url=_BASE_URL,
    )


# ---------------------------------------------------------------------------
# 1. pass_through — no profile registered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_pass_through_no_profile():
    """When no profile is registered, request reaches the echo app unmodified."""
    svc = _make_service()
    async with _make_client(svc) as client:
        resp = await client.post(_ECHO_PATH)
    assert resp.status_code == _HTTP_STATUS_OK
    assert json.loads(resp.content) == {"echo": "ok"}


# ---------------------------------------------------------------------------
# 2. http_error — synthetic non-2xx response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_http_error():
    """error_rate=1.0 → synthetic 502 response with retry-after header."""
    svc = _make_service()
    svc.register_profile(
        _TARGET,
        _make_profile(
            error_rate=1.0,
            error_codes=[_ERROR_CODE_502],
            retry_after_sec_range=(_RETRY_AFTER_MIN, _RETRY_AFTER_MAX),
        ),
    )
    async with _make_client(svc) as client:
        resp = await client.post(_ECHO_PATH)
    assert resp.status_code == _ERROR_CODE_502
    assert "retry-after" in resp.headers
    assert isinstance(resp, httpx.Response)


# ---------------------------------------------------------------------------
# 3. connect_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_connect_timeout():
    """connect_timeout_rate=1.0 → httpx.ConnectTimeout raised."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(connect_timeout_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 4. read_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_read_timeout():
    """read_timeout_rate=1.0 → httpx.ReadTimeout raised."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(read_timeout_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 5. write_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_write_timeout():
    """write_timeout_rate=1.0 → httpx.WriteTimeout raised."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(write_timeout_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.WriteTimeout):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 6. pool_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_pool_timeout():
    """pool_timeout_rate=1.0 → httpx.PoolTimeout raised."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(pool_timeout_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.PoolTimeout):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 7. connect_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_connect_error():
    """connect_error_rate=1.0 → httpx.ConnectError raised."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(connect_error_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 8. dns_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_dns_failure():
    """dns_failure_rate=1.0 → httpx.ConnectError raised (caused by socket.gaierror)."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(dns_failure_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 9. tls_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_tls_error():
    """tls_error_rate=1.0 → httpx.ConnectError raised (caused by ssl.SSLError)."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(tls_error_rate=1.0))
    async with _make_client(svc) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_ECHO_PATH)


# ---------------------------------------------------------------------------
# 10. malformed_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_malformed_json():
    """malformed_rate=1.0 → 200 response whose body fails JSON parsing."""
    svc = _make_service()
    svc.register_profile(
        _TARGET,
        _make_profile(malformed_rate=1.0, corruption_modes=["truncate"]),
    )
    async with _make_client(svc) as client:
        resp = await client.post(_ECHO_PATH)
    assert resp.status_code == _HTTP_STATUS_OK
    with pytest.raises((json.JSONDecodeError, ValueError, UnicodeDecodeError)):
        json.loads(resp.content)


# ---------------------------------------------------------------------------
# 11. redirect_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_redirect_loop():
    """redirect_loop_rate=1.0 → 302 with Location pointing at original request URL."""
    svc = _make_service()
    svc.register_profile(_TARGET, _make_profile(redirect_loop_rate=1.0))
    async with _make_client(svc) as client:
        resp = await client.post(_ECHO_PATH, follow_redirects=False)
    assert resp.status_code == _HTTP_STATUS_REDIRECT
    assert "location" in resp.headers
    assert _ECHO_PATH in resp.headers["location"]


# ---------------------------------------------------------------------------
# 12. stream_disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_stream_disconnect():
    """stream_disconnect_rate=1.0 → 200 then httpx.StreamError when reading body.

    Uses client.stream() to prevent httpx from auto-reading the response body
    on send(), so the injected StreamError fires only when we explicitly call
    resp.aread() inside the streaming context.
    """
    svc = _make_service()
    svc.register_profile(
        _TARGET,
        _make_profile(
            stream_disconnect_rate=1.0,
            truncate_after_bytes_range=(_TRUNCATE_BYTES, _TRUNCATE_BYTES),
        ),
    )
    fault_transport = FaultInjectingTransport(
        wrapped=httpx.ASGITransport(app=_large_echo_app),
        service=svc,
    )
    async with httpx.AsyncClient(
        transport=fault_transport, base_url=_BASE_URL
    ) as client:
        async with client.stream("POST", _ECHO_PATH) as resp:
            assert resp.status_code == _HTTP_STATUS_OK
            # Body read must fail with the injected stream error
            with pytest.raises(httpx.StreamError):
                await resp.aread()


# ---------------------------------------------------------------------------
# 13. latency (additive, non-terminating) — real asyncio.sleep path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_latency_additive():
    """latency_rate=1.0 → real asyncio.sleep delay applied; response still succeeds.

    Uses the default asyncio.sleep path (no injected sleep_fn).
    Elapsed time must be at least _LATENCY_TOLERANCE_FACTOR * configured_delay_seconds
    to confirm the sleep actually ran.
    """
    svc = _make_service()
    svc.register_profile(
        _TARGET,
        _make_profile(
            latency_rate=1.0,
            latency_ms_range=(_LATENCY_CONSTANT_MS, _LATENCY_CONSTANT_MS),
        ),
    )
    expected_delay_seconds = _LATENCY_CONSTANT_MS / 1000.0
    minimum_elapsed = expected_delay_seconds * _LATENCY_TOLERANCE_FACTOR

    start = time.monotonic()
    async with _make_client(svc) as client:
        resp = await client.post(_ECHO_PATH)
    elapsed = time.monotonic() - start

    assert resp.status_code == _HTTP_STATUS_OK
    assert elapsed >= minimum_elapsed, (
        f"Expected at least {minimum_elapsed:.3f}s elapsed, got {elapsed:.3f}s; "
        "latency injection may not have fired"
    )
    latency_events = [e for e in svc.get_history() if e.fault_type == "latency"]
    assert len(latency_events) == 1
