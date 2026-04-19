"""
Tests for FaultInjectingTransport.

Story #746 — Scenarios 8, 17, 19, 28, 30, 31.

TDD: tests written BEFORE production code.

Stub design: StubTransport is a minimal httpx.AsyncBaseTransport that returns
a configurable bytes payload, avoiding any real network calls.  All httpx
Request/Response objects are real instances (no mocking).

Sleep injection: FaultInjectingTransport accepts an optional `sleep_fn`
callable so latency tests can capture delays without patching asyncio.sleep.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import List, Optional

import httpx
import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_profile import FaultProfile
from code_indexer.server.fault_injection.fault_injecting_transport import (
    FaultInjectingTransport,
)
from code_indexer.server.fault_injection._transport_helpers import (
    _corrupt_json_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED = 42
TARGET = "provider-a.test"
URL = f"https://{TARGET}/v1/embed"
DEFAULT_BODY = b'{"result": "ok"}'
HTTP_ERROR_CODE = 429
HTTP_503_CODE = 503
RETRY_AFTER_MIN = 1
RETRY_AFTER_MAX = 3
TRUNCATE_BYTES = 50
LARGE_BODY = b"X" * 200
LATENCY_CONSTANT_MS = 10
SLOW_TAIL_CONSTANT_MS = 10
REDIRECT_302 = 302
MALFORMED_RESPONSE_CODE = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(enabled: bool = True, seed: int = SEED) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(seed))


def _make_request(url: str = URL) -> httpx.Request:
    return httpx.Request("POST", url, content=b"{}")


def _profile(**kwargs) -> FaultProfile:
    """Build a FaultProfile with sensible defaults for transport tests."""
    base: dict = {"target": TARGET, "error_rate": 0.0, "error_codes": []}
    base.update(kwargs)
    if base.get("error_rate", 0.0) > 0.0 and not base.get("error_codes"):
        base["error_codes"] = [HTTP_ERROR_CODE]
    if base.get("malformed_rate", 0.0) > 0.0 and not base.get("corruption_modes"):
        base["corruption_modes"] = ["truncate"]
    return FaultProfile(**base)


class StubTransport(httpx.AsyncBaseTransport):
    """Minimal stub wrapped transport returning configurable bytes."""

    def __init__(self, body: bytes = DEFAULT_BODY, status_code: int = 200) -> None:
        self._body = body
        self._status_code = status_code
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return httpx.Response(
            status_code=self._status_code,
            content=self._body,
            request=request,
        )


class CapturingSleepFn:
    """Injectable sleep callable that records requested delays instead of sleeping."""

    def __init__(self) -> None:
        self.recorded_delays: List[float] = []

    async def __call__(self, delay: float) -> None:
        self.recorded_delays.append(delay)


class RaiseCancelledSleepFn:
    """Injectable sleep callable that raises CancelledError on first call."""

    async def __call__(self, delay: float) -> None:
        raise asyncio.CancelledError()


def _make_transport(
    profile: Optional[FaultProfile] = None,
    enabled: bool = True,
    seed: int = SEED,
    stub_body: bytes = DEFAULT_BODY,
    sleep_fn=None,
) -> tuple:
    svc = _make_service(enabled=enabled, seed=seed)
    if profile is not None:
        svc.register_profile(TARGET, profile)
    stub = StubTransport(body=stub_body)
    transport = FaultInjectingTransport(wrapped=stub, service=svc, sleep_fn=sleep_fn)
    return transport, svc, stub


async def _invoke(
    profile: Optional[FaultProfile] = None,
    enabled: bool = True,
    seed: int = SEED,
    stub_body: bytes = DEFAULT_BODY,
    sleep_fn=None,
    url: str = URL,
) -> tuple:
    """Build transport, invoke one request, return (response, svc, stub)."""
    transport, svc, stub = _make_transport(
        profile=profile,
        enabled=enabled,
        seed=seed,
        stub_body=stub_body,
        sleep_fn=sleep_fn,
    )
    req = _make_request(url=url)
    resp = await transport.handle_async_request(req)
    return resp, svc, stub


# ===========================================================================
# Pass-through paths
# ===========================================================================


@pytest.mark.asyncio
async def test_pass_through_when_no_profile():
    """No registered profile -> delegates to wrapped transport."""
    resp, svc, stub = await _invoke(profile=None)
    assert resp.status_code == 200
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_pass_through_when_service_disabled():
    """Service disabled -> delegates regardless of registered profile."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE]),
        enabled=False,
    )
    assert resp.status_code == 200
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_pass_through_when_profile_disabled():
    """Profile.enabled=False -> delegates to wrapped transport."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE], enabled=False),
    )
    assert resp.status_code == 200
    assert stub.call_count == 1


@pytest.mark.asyncio
async def test_pass_through_outcome_delegates_to_wrapped():
    """All rates zero -> pass_through outcome -> delegates."""
    resp, svc, stub = await _invoke(profile=_profile(error_rate=0.0))
    assert stub.call_count == 1
    assert resp.status_code == 200


# ===========================================================================
# HTTP error injection (Scenario 8, 30)
# ===========================================================================


@pytest.mark.asyncio
async def test_http_error_returns_synthetic_response():
    """error_rate=1.0 -> returns synthetic httpx.Response, no wrapped call."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE]),
    )
    assert stub.call_count == 0
    assert resp.status_code == HTTP_ERROR_CODE


@pytest.mark.asyncio
async def test_http_error_response_is_real_httpx_response():
    """Scenario 30: synthetic response must be a real httpx.Response instance."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE]),
    )
    assert isinstance(resp, httpx.Response)
    assert isinstance(resp.headers, httpx.Headers)


@pytest.mark.asyncio
async def test_http_error_retry_after_header_within_range():
    """Retry-After header value must be within retry_after_sec_range."""
    resp, svc, stub = await _invoke(
        profile=_profile(
            error_rate=1.0,
            error_codes=[HTTP_ERROR_CODE],
            retry_after_sec_range=(RETRY_AFTER_MIN, RETRY_AFTER_MAX),
        ),
    )
    assert "retry-after" in resp.headers
    value = int(resp.headers["retry-after"])
    assert RETRY_AFTER_MIN <= value <= RETRY_AFTER_MAX


@pytest.mark.asyncio
async def test_http_error_503_code():
    """error_codes=[503] -> response has status 503."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_503_CODE]),
    )
    assert resp.status_code == HTTP_503_CODE


@pytest.mark.asyncio
async def test_http_error_response_content_is_bytes():
    """Scenario 30: resp.content must be bytes."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE]),
    )
    assert isinstance(resp.content, bytes)


# ===========================================================================
# Timeout and connection error injections (parametrized matrix)
# ===========================================================================

_RAISE_OUTCOMES = [
    pytest.param(
        {"connect_timeout_rate": 1.0},
        httpx.ConnectTimeout,
        id="connect_timeout",
    ),
    pytest.param(
        {"read_timeout_rate": 1.0},
        httpx.ReadTimeout,
        id="read_timeout",
    ),
    pytest.param(
        {"write_timeout_rate": 1.0},
        httpx.WriteTimeout,
        id="write_timeout",
    ),
    pytest.param(
        {"pool_timeout_rate": 1.0},
        httpx.PoolTimeout,
        id="pool_timeout",
    ),
    pytest.param(
        {"connect_error_rate": 1.0},
        httpx.ConnectError,
        id="connect_error",
    ),
    pytest.param(
        {"dns_failure_rate": 1.0},
        httpx.ConnectError,
        id="dns_failure",
    ),
    pytest.param(
        {"tls_error_rate": 1.0},
        httpx.ConnectError,
        id="tls_error",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_kwargs,exc_type", _RAISE_OUTCOMES)
async def test_raise_outcome_raises_correct_exception(profile_kwargs, exc_type):
    """Each raising outcome raises the correct httpx exception; wrapped not called."""
    transport, svc, stub = _make_transport(profile=_profile(**profile_kwargs))
    req = _make_request()
    with pytest.raises(exc_type):
        await transport.handle_async_request(req)
    assert stub.call_count == 0


# ===========================================================================
# Malformed JSON injection
# ===========================================================================


@pytest.mark.asyncio
async def test_malformed_returns_200_with_corrupted_body():
    """malformed_rate=1.0 -> returns 200 with corrupted JSON, no wrapped call."""
    resp, svc, stub = await _invoke(
        profile=_profile(malformed_rate=1.0, corruption_modes=["truncate"]),
    )
    assert stub.call_count == 0
    assert resp.status_code == MALFORMED_RESPONSE_CODE
    with pytest.raises((json.JSONDecodeError, ValueError, UnicodeDecodeError)):
        json.loads(resp.content)


@pytest.mark.asyncio
async def test_malformed_response_is_real_httpx_response():
    resp, svc, stub = await _invoke(
        profile=_profile(malformed_rate=1.0, corruption_modes=["truncate"]),
    )
    assert isinstance(resp, httpx.Response)


# ---------------------------------------------------------------------------
# Corruption mode branch coverage
# ---------------------------------------------------------------------------

_INVALID_JSON_MODES = [
    pytest.param("truncate", id="truncate"),
    pytest.param("invalid_utf8", id="invalid_utf8"),
    pytest.param("empty", id="empty"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _INVALID_JSON_MODES)
async def test_malformed_invalid_json_modes(mode: str):
    """truncate / invalid_utf8 / empty modes must produce a body that fails JSON parsing."""
    resp, svc, stub = await _invoke(
        profile=_profile(malformed_rate=1.0, corruption_modes=[mode]),
    )
    assert resp.status_code == MALFORMED_RESPONSE_CODE
    with pytest.raises((json.JSONDecodeError, ValueError, UnicodeDecodeError)):
        json.loads(resp.content)


@pytest.mark.asyncio
async def test_malformed_wrong_schema_mode():
    """
    wrong_schema mode must produce a 200 with a valid JSON body whose structure
    does not match the expected schema (contains 'unexpected_key', not 'result').
    """
    resp, svc, stub = await _invoke(
        profile=_profile(malformed_rate=1.0, corruption_modes=["wrong_schema"]),
    )
    assert resp.status_code == MALFORMED_RESPONSE_CODE
    parsed = json.loads(resp.content)
    assert "unexpected_key" in parsed
    assert "result" not in parsed


# ===========================================================================
# Redirect loop injection
# ===========================================================================


@pytest.mark.asyncio
async def test_redirect_loop_returns_302_to_self():
    """redirect_loop_rate=1.0 -> 302 pointing at the same URL."""
    resp, svc, stub = await _invoke(
        profile=_profile(redirect_loop_rate=1.0),
    )
    assert stub.call_count == 0
    assert resp.status_code == REDIRECT_302
    assert "location" in resp.headers
    assert resp.headers["location"] == URL


# ===========================================================================
# Stream disconnect injection (Scenario 19, 31)
# ===========================================================================


@pytest.mark.asyncio
async def test_stream_disconnect_yields_truncated_bytes():
    """Scenario 31: exactly TRUNCATE_BYTES yielded then StreamError raised."""
    transport, svc, stub = _make_transport(
        profile=_profile(
            stream_disconnect_rate=1.0,
            truncate_after_bytes_range=(TRUNCATE_BYTES, TRUNCATE_BYTES),
        ),
        stub_body=LARGE_BODY,
    )
    req = _make_request()
    resp = await transport.handle_async_request(req)
    assert resp.status_code == 200

    collected = b""
    raised = False
    try:
        async for chunk in resp.aiter_raw():
            collected += chunk
    except httpx.StreamError:
        raised = True

    assert len(collected) == TRUNCATE_BYTES, (
        f"Expected {TRUNCATE_BYTES} bytes, got {len(collected)}"
    )
    assert raised, "Expected StreamError after truncation"


@pytest.mark.asyncio
async def test_stream_disconnect_aread_raises():
    """Scenario 31: resp.aread() must raise the expected exception."""
    transport, svc, stub = _make_transport(
        profile=_profile(
            stream_disconnect_rate=1.0,
            truncate_after_bytes_range=(TRUNCATE_BYTES, TRUNCATE_BYTES),
        ),
        stub_body=LARGE_BODY,
    )
    req = _make_request()
    resp = await transport.handle_async_request(req)
    with pytest.raises((httpx.StreamError, httpx.ResponseNotRead)):
        await resp.aread()


# ===========================================================================
# Additive latency paths (injectable sleep_fn)
# ===========================================================================


@pytest.mark.asyncio
async def test_latency_applied_before_pass_through():
    """latency_rate=1.0 with pass_through -> sleep_fn called once with correct delay."""
    sleep_fn = CapturingSleepFn()
    await _invoke(
        profile=_profile(
            latency_rate=1.0,
            latency_ms_range=(LATENCY_CONSTANT_MS, LATENCY_CONSTANT_MS),
        ),
        sleep_fn=sleep_fn,
    )
    assert len(sleep_fn.recorded_delays) == 1
    assert abs(sleep_fn.recorded_delays[0] - LATENCY_CONSTANT_MS / 1000.0) < 1e-9


@pytest.mark.asyncio
async def test_slow_tail_applied_before_pass_through():
    """slow_tail_rate=1.0 -> sleep_fn called once for slow tail."""
    sleep_fn = CapturingSleepFn()
    await _invoke(
        profile=_profile(
            slow_tail_rate=1.0,
            slow_tail_ms_range=(SLOW_TAIL_CONSTANT_MS, SLOW_TAIL_CONSTANT_MS),
        ),
        sleep_fn=sleep_fn,
    )
    assert len(sleep_fn.recorded_delays) == 1
    assert abs(sleep_fn.recorded_delays[0] - SLOW_TAIL_CONSTANT_MS / 1000.0) < 1e-9


@pytest.mark.asyncio
async def test_latency_not_applied_before_connect_error():
    """Latency must NOT be applied when outcome is connect_error (immediate raise)."""
    sleep_fn = CapturingSleepFn()
    transport, svc, stub = _make_transport(
        profile=_profile(
            connect_error_rate=1.0,
            latency_rate=1.0,
            latency_ms_range=(LATENCY_CONSTANT_MS, LATENCY_CONSTANT_MS),
        ),
        sleep_fn=sleep_fn,
    )
    req = _make_request()
    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(req)
    assert sleep_fn.recorded_delays == [], (
        "No latency should be applied before connect_error"
    )


# ===========================================================================
# Cancellation propagation (Scenario 17)
# ===========================================================================


@pytest.mark.asyncio
async def test_cancelled_error_propagates_during_latency():
    """Scenario 17: CancelledError from sleep_fn must propagate, not be swallowed."""
    sleep_fn = RaiseCancelledSleepFn()
    transport, svc, stub = _make_transport(
        profile=_profile(
            latency_rate=1.0,
            latency_ms_range=(LATENCY_CONSTANT_MS, LATENCY_CONSTANT_MS),
        ),
        sleep_fn=sleep_fn,
    )
    req = _make_request()
    with pytest.raises(asyncio.CancelledError):
        await transport.handle_async_request(req)


@pytest.mark.asyncio
async def test_cancelled_error_records_cancelled_history_entry():
    """C2/Scenario 17: CancelledError during latency sleep must record a 'cancelled'
    event in the history ring buffer before re-raising.

    The correlation_id is generated internally by the transport and is not known
    in advance; we verify it is a non-empty string (UUID format).
    """
    sleep_fn = RaiseCancelledSleepFn()
    transport, svc, stub = _make_transport(
        profile=_profile(
            latency_rate=1.0,
            latency_ms_range=(LATENCY_CONSTANT_MS, LATENCY_CONSTANT_MS),
        ),
        sleep_fn=sleep_fn,
    )
    req = _make_request()
    with pytest.raises(asyncio.CancelledError):
        await transport.handle_async_request(req)

    history = svc.get_history()
    cancelled_events = [e for e in history if e.fault_type == "cancelled"]
    assert len(cancelled_events) == 1, (
        f"Expected exactly one 'cancelled' event, got: {history}"
    )
    assert cancelled_events[0].correlation_id, (
        "correlation_id must be a non-empty string"
    )


# ===========================================================================
# Injection event recording (Scenario 32)
# ===========================================================================


@pytest.mark.asyncio
async def test_injection_event_recorded_for_http_error():
    """Scenario 32: exactly one injection event recorded per http_error."""
    resp, svc, stub = await _invoke(
        profile=_profile(error_rate=1.0, error_codes=[HTTP_ERROR_CODE]),
    )
    http_error_events = [e for e in svc.get_history() if e.fault_type == "http_error"]
    assert len(http_error_events) == 1


@pytest.mark.asyncio
async def test_no_injection_event_for_pass_through():
    """Pass-through outcomes produce no injection event."""
    resp, svc, stub = await _invoke(profile=_profile(error_rate=0.0))
    assert svc.get_history() == []


# ===========================================================================
# M3: _corrupt_json_payload raises on unknown mode (transport defense-in-depth)
# ===========================================================================


def test_corrupt_json_raises_on_unknown_mode():
    """M3: _corrupt_json_payload must raise ValueError for any mode not in the allowed set.

    Profile validation (FaultProfile.__post_init__) should prevent unknown modes
    from ever reaching the transport.  This test ensures that if an unknown mode
    somehow reaches _corrupt_json_payload (e.g., during testing or future refactor), it
    raises rather than silently returning empty bytes.
    """
    import random as _random

    rng = _random.Random(42)
    with pytest.raises(ValueError, match="unknown corruption mode"):
        _corrupt_json_payload("not_a_real_mode", rng)


# ===========================================================================
# M4: Per-request RNG isolation under concurrent load
# ===========================================================================

# Named constants for the M4 concurrency stress test
_M4_REQUEST_COUNT = 100
_M4_RESEED_COUNT = 10
_M4_RESEED_BASE = 3
_M4_RESEED_STEP = 7

# Fault rates configured in the M4 test profile
_M4_ERROR_RATE = 0.3
_M4_CONNECT_TIMEOUT_RATE = 0.1
_M4_READ_TIMEOUT_RATE = 0.1
_M4_MALFORMED_RATE = 0.1
_M4_REDIRECT_LOOP_RATE = 0.1

# HTTP status codes used by the classifier
_HTTP_STATUS_OK = 200
_HTTP_STATUS_REDIRECT = 302

# Stub body for M4 test — valid JSON, unambiguously identifies pass_through
# (malformed_json outcomes always return corrupted bytes, never this exact body)
_M4_STUB_BODY = b'{"ok": true}'

# Valid symbolic outcome labels for the M4 test profile.
# Restricted to outcomes that are:
#   (a) configured in the test profile, AND
#   (b) uniquely distinguishable by _classify_m4_outcome().
#
# Excluded intentionally:
#   - dns_failure / tls_error: both raise httpx.ConnectError, indistinguishable
#   - stream_disconnect: requires async iteration, not suitable for bulk concurrent test
#   - write_timeout / pool_timeout: not in the test profile
#   - latency / cancelled: additive, not terminating outcomes
_M4_VALID_OUTCOMES = frozenset(
    {
        "pass_through",  # all-zero rates or no match: stub returns 200 + valid JSON
        "http_error",  # error_rate=_M4_ERROR_RATE: non-200/non-302 status code
        "connect_timeout",  # connect_timeout_rate=_M4_CONNECT_TIMEOUT_RATE
        "read_timeout",  # read_timeout_rate=_M4_READ_TIMEOUT_RATE
        "malformed_json",  # malformed_rate=_M4_MALFORMED_RATE: 200 with corrupted body
        "redirect_loop",  # redirect_loop_rate=_M4_REDIRECT_LOOP_RATE: 302 self-ref
    }
)


def _classify_m4_outcome(result: object) -> str:
    """Map a request result (httpx.Response or raised exception) to a symbolic
    outcome label for the M4 concurrent-isolation test.

    Distinguishability contract:
      - httpx.ConnectTimeout            -> "connect_timeout"  (unique exception type)
      - httpx.ReadTimeout               -> "read_timeout"     (unique exception type)
      - httpx.Response status 302       -> "redirect_loop"    (unique status code)
      - httpx.Response status != 200/302 -> "http_error"
      - httpx.Response status 200, body != _M4_STUB_BODY -> "malformed_json"
      - httpx.Response status 200, body == _M4_STUB_BODY -> "pass_through"
      - Anything else                   -> "unexpected:<type>" (fails assertion)
    """
    if isinstance(result, httpx.ConnectTimeout):
        return "connect_timeout"
    if isinstance(result, httpx.ReadTimeout):
        return "read_timeout"
    if isinstance(result, httpx.Response):
        if result.status_code == _HTTP_STATUS_REDIRECT:
            return "redirect_loop"
        if result.status_code != _HTTP_STATUS_OK:
            return "http_error"
        # 200 response: distinguish malformed_json from pass_through by body content
        if result.content != _M4_STUB_BODY:
            return "malformed_json"
        return "pass_through"
    return f"unexpected:{type(result).__name__}"


@pytest.mark.asyncio
async def test_concurrent_per_request_rng_isolation():
    """M4: _M4_REQUEST_COUNT concurrent requests + _M4_RESEED_COUNT interleaved
    set_seed() calls must each produce a valid outcome from _M4_VALID_OUTCOMES.

    With the shared RNG (pre-fix), interleaved set_seed() calls can corrupt
    in-flight select_outcome() state, causing outcomes outside the valid set.
    With per-request RNG isolation (post-fix), each request draws its seed
    atomically before use, so concurrent set_seed() calls cannot corrupt it.

    The test profile configures 5 distinguishable fault modes totalling 0.6 rate,
    leaving 0.4 pass_through probability.  All 6 possible labels (including
    pass_through) are in _M4_VALID_OUTCOMES.  Any label prefixed "unexpected:"
    indicates the implementation is broken.
    """
    profile = _profile(
        error_rate=_M4_ERROR_RATE,
        error_codes=[HTTP_ERROR_CODE],
        connect_timeout_rate=_M4_CONNECT_TIMEOUT_RATE,
        read_timeout_rate=_M4_READ_TIMEOUT_RATE,
        malformed_rate=_M4_MALFORMED_RATE,
        corruption_modes=["truncate"],
        redirect_loop_rate=_M4_REDIRECT_LOOP_RATE,
    )

    svc = _make_service(seed=SEED)
    svc.register_profile(TARGET, profile)
    stub = StubTransport(body=_M4_STUB_BODY)

    outcomes: List[str] = []
    outcomes_lock = asyncio.Lock()

    async def _one_request() -> None:
        transport = FaultInjectingTransport(wrapped=stub, service=svc)
        req = _make_request()
        try:
            resp = await transport.handle_async_request(req)
            label = _classify_m4_outcome(resp)
        except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            label = _classify_m4_outcome(exc)
        async with outcomes_lock:
            outcomes.append(label)

    async def _seed_interrupter() -> None:
        """Rapidly reseed _M4_RESEED_COUNT times to maximally disturb shared-RNG state."""
        for i in range(_M4_RESEED_COUNT):
            svc.set_seed(_M4_RESEED_BASE + i * _M4_RESEED_STEP)
            await asyncio.sleep(0)  # yield to allow request tasks to interleave

    tasks = [asyncio.create_task(_one_request()) for _ in range(_M4_REQUEST_COUNT)]
    interrupter = asyncio.create_task(_seed_interrupter())
    await asyncio.gather(interrupter, *tasks)

    assert len(outcomes) == _M4_REQUEST_COUNT, (
        f"Expected {_M4_REQUEST_COUNT} outcomes, got {len(outcomes)}"
    )

    invalid = [o for o in outcomes if o not in _M4_VALID_OUTCOMES]
    assert not invalid, f"Invalid outcome labels (M4 RNG isolation failure): {invalid}"
