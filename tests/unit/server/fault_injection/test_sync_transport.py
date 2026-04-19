"""
Tests for FaultInjectingSyncTransport.

Story #746 — C1 sync transport architectural fix.

Mirrors test_transport.py for the sync counterpart.

StubSyncTransport is a minimal httpx.BaseTransport that returns a configurable
bytes payload without any real network calls.  All httpx Request/Response
objects are real instances (no mocking).

Coverage targets:
  1.  no-profile pass_through
  2.  http_error
  3-6. timeout outcomes (connect_timeout, read_timeout, write_timeout, pool_timeout)
  7-9. error outcomes (connect_error, dns_failure, tls_error)
  10. malformed_json
  11. redirect_loop
  12. stream_disconnect
  13. latency additive (time.sleep path)
  14. close() delegation to wrapped transport
  15. disabled service — unconditional pass_through
  16. injection history recorded for terminating fault
  17. no history recorded for pass_through outcome
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
from code_indexer.server.fault_injection.fault_injecting_sync_transport import (
    FaultInjectingSyncTransport,
)
from code_indexer.server.fault_injection.fault_profile import FaultProfile

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED = 42
TARGET = "provider-sync.test"
URL = f"https://{TARGET}/v1/embed"
DEFAULT_BODY = b'{"result": "ok"}'
LARGE_BODY = b"Y" * 200
HTTP_ERROR_CODE = 429
RETRY_AFTER_MIN = 1
RETRY_AFTER_MAX = 3
TRUNCATE_BYTES = 50
LATENCY_CONSTANT_MS = 10
REDIRECT_302 = 302


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(enabled: bool = True, seed: int = SEED) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(seed))


def _make_request(url: str = URL) -> httpx.Request:
    return httpx.Request("POST", url, content=b"{}")


def _profile(**kwargs) -> FaultProfile:
    base: dict = {"target": TARGET, "error_rate": 0.0, "error_codes": []}
    base.update(kwargs)
    if base.get("error_rate", 0.0) > 0.0 and not base.get("error_codes"):
        base["error_codes"] = [HTTP_ERROR_CODE]
    if base.get("malformed_rate", 0.0) > 0.0 and not base.get("corruption_modes"):
        base["corruption_modes"] = ["truncate"]
    return FaultProfile(**base)


class StubSyncTransport(httpx.BaseTransport):
    """Minimal stub sync transport returning configurable bytes."""

    def __init__(self, body: bytes = DEFAULT_BODY) -> None:
        self._body = body
        self.close_called = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=self._body, request=request)

    def close(self) -> None:
        self.close_called = True


def _make_transport(
    svc: FaultInjectionService,
    body: bytes = DEFAULT_BODY,
) -> tuple:
    stub = StubSyncTransport(body=body)
    transport = FaultInjectingSyncTransport(wrapped=stub, service=svc)
    return transport, stub


# ---------------------------------------------------------------------------
# 1. No profile registered — pass_through
# ---------------------------------------------------------------------------


def test_pass_through_when_no_profile():
    """When no profile is registered, the request is forwarded unchanged."""
    svc = _make_service()
    transport, _ = _make_transport(svc)
    req = _make_request()
    resp = transport.handle_request(req)
    assert resp.status_code == 200
    assert resp.content == DEFAULT_BODY


# ---------------------------------------------------------------------------
# 2. http_error
# ---------------------------------------------------------------------------


def test_http_error_returns_synthetic_response():
    """error_rate=1.0 → synthetic error response with retry-after header."""
    svc = _make_service()
    svc.register_profile(
        TARGET,
        _profile(
            error_rate=1.0,
            error_codes=[HTTP_ERROR_CODE],
            retry_after_sec_range=(RETRY_AFTER_MIN, RETRY_AFTER_MAX),
        ),
    )
    transport, _ = _make_transport(svc)
    req = _make_request()
    resp = transport.handle_request(req)
    assert resp.status_code == HTTP_ERROR_CODE
    assert "retry-after" in resp.headers


# ---------------------------------------------------------------------------
# 3–6. Timeout outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rate_field,exc_type",
    [
        ("connect_timeout_rate", httpx.ConnectTimeout),
        ("read_timeout_rate", httpx.ReadTimeout),
        ("write_timeout_rate", httpx.WriteTimeout),
        ("pool_timeout_rate", httpx.PoolTimeout),
    ],
)
def test_timeout_outcomes_raise_correct_exception(rate_field, exc_type):
    """Timeout outcomes raise the corresponding httpx exception."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile(**{rate_field: 1.0}))
    transport, _ = _make_transport(svc)
    req = _make_request()
    with pytest.raises(exc_type):
        transport.handle_request(req)


# ---------------------------------------------------------------------------
# 7–9. Error outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rate_field",
    ["connect_error_rate", "dns_failure_rate", "tls_error_rate"],
)
def test_error_outcomes_raise_connect_error(rate_field):
    """connect_error, dns_failure, tls_error all raise httpx.ConnectError."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile(**{rate_field: 1.0}))
    transport, _ = _make_transport(svc)
    req = _make_request()
    with pytest.raises(httpx.ConnectError):
        transport.handle_request(req)


# ---------------------------------------------------------------------------
# 10. malformed_json
# ---------------------------------------------------------------------------


def _make_malformed_response(corruption_mode: str) -> bytes:
    """Return the raw response content for a given corruption mode via the transport."""
    svc = _make_service()
    svc.register_profile(
        TARGET,
        _profile(malformed_rate=1.0, corruption_modes=[corruption_mode]),
    )
    transport, _ = _make_transport(svc)
    resp: httpx.Response = transport.handle_request(_make_request())
    assert resp.status_code == 200
    return resp.content


@pytest.mark.parametrize(
    "mode,expect_invalid_json",
    [
        pytest.param("truncate", True, id="truncate"),
        pytest.param("invalid_utf8", True, id="invalid_utf8"),
        pytest.param("empty", True, id="empty"),
        pytest.param("wrong_schema", False, id="wrong_schema"),
    ],
)
def test_malformed_json_all_modes(mode, expect_invalid_json):
    """All corruption modes produce a 200; truncate/invalid_utf8/empty fail JSON parsing;
    wrong_schema produces valid JSON with an unexpected key."""
    content = _make_malformed_response(mode)
    if expect_invalid_json:
        with pytest.raises((json.JSONDecodeError, ValueError, UnicodeDecodeError)):
            json.loads(content)
    else:
        body = json.loads(content)
        assert "unexpected_key" in body


# ---------------------------------------------------------------------------
# 11. redirect_loop
# ---------------------------------------------------------------------------


def test_redirect_loop_returns_302_to_self():
    """redirect_loop_rate=1.0 → 302 with Location pointing at original URL."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile(redirect_loop_rate=1.0))
    transport, _ = _make_transport(svc)
    req = _make_request()
    resp = transport.handle_request(req)
    assert resp.status_code == REDIRECT_302
    assert "location" in resp.headers
    assert TARGET in resp.headers["location"]


# ---------------------------------------------------------------------------
# 12. stream_disconnect
# ---------------------------------------------------------------------------


def test_stream_disconnect_returns_truncating_response():
    """stream_disconnect_rate=1.0 → response whose body raises StreamError."""
    svc = _make_service()
    svc.register_profile(
        TARGET,
        _profile(
            stream_disconnect_rate=1.0,
            truncate_after_bytes_range=(TRUNCATE_BYTES, TRUNCATE_BYTES),
        ),
    )
    transport, _ = _make_transport(svc, body=LARGE_BODY)
    req = _make_request()
    resp = transport.handle_request(req)
    assert resp.status_code == 200
    with pytest.raises(httpx.StreamError):
        resp.read()


# ---------------------------------------------------------------------------
# 13. latency (additive, sync time.sleep path)
# ---------------------------------------------------------------------------


def test_latency_applied_before_pass_through():
    """latency_rate=1.0 → time.sleep called, response still succeeds."""
    svc = _make_service()
    svc.register_profile(
        TARGET,
        _profile(
            latency_rate=1.0,
            latency_ms_range=(LATENCY_CONSTANT_MS, LATENCY_CONSTANT_MS),
        ),
    )
    transport, _ = _make_transport(svc)
    req = _make_request()
    start = time.monotonic()
    resp = transport.handle_request(req)
    elapsed = time.monotonic() - start
    assert resp.status_code == 200
    # At least half the configured delay should have elapsed
    assert elapsed >= (LATENCY_CONSTANT_MS / 1000.0) * 0.5
    latency_events = [e for e in svc.get_history() if e.fault_type == "latency"]
    assert len(latency_events) == 1


# ---------------------------------------------------------------------------
# 14. close() delegation
# ---------------------------------------------------------------------------


def test_close_delegates_to_wrapped_transport():
    """close() must call close() on the wrapped transport."""
    svc = _make_service()
    transport, stub = _make_transport(svc)
    transport.close()
    assert stub.close_called


# ---------------------------------------------------------------------------
# 15. Disabled service — unconditional pass_through
# ---------------------------------------------------------------------------


def test_disabled_service_passes_through():
    """When service is disabled, all requests are forwarded unchanged."""
    svc = _make_service(enabled=False)
    svc.register_profile(TARGET, _profile(connect_error_rate=1.0))
    transport, _ = _make_transport(svc)
    req = _make_request()
    resp = transport.handle_request(req)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 16. Injection history recorded for terminating fault
# ---------------------------------------------------------------------------


def test_injection_event_recorded_for_http_error():
    """A fault event is recorded in service history on http_error."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile(error_rate=1.0))
    transport, _ = _make_transport(svc)
    req = _make_request()
    transport.handle_request(req)
    history = svc.get_history()
    assert len(history) == 1
    assert history[0].fault_type == "http_error"
    assert history[0].target == TARGET


# ---------------------------------------------------------------------------
# 17. No history recorded for pass_through outcome
# ---------------------------------------------------------------------------


def test_no_injection_event_for_pass_through():
    """pass_through outcome records no injection event."""
    svc = _make_service()
    svc.register_profile(TARGET, _profile())
    transport, _ = _make_transport(svc)
    req = _make_request()
    transport.handle_request(req)
    assert svc.get_history() == []
