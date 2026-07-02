"""
FaultInjectingTransport — httpx.AsyncBaseTransport subclass that intercepts
outbound requests and applies configurable fault modes per target.

Story #746 — Phase B transport layer.

Design notes:
  - All randomness goes through service.rng (injectable for determinism).
  - sleep_fn is injectable (defaults to asyncio.sleep) so tests can capture
    or cancel delays without patching module-level names.
  - CancelledError from sleep_fn propagates unconditionally (Scenario 17).
  - Additive latency/slow_tail are skipped for ALL immediate-raise outcomes:
    connect_timeout, read_timeout, write_timeout, pool_timeout,
    connect_error, dns_failure, tls_error.
  - pass_through records no injection event.
  - Latency and slow_tail each record a separate injection event with their
    own fault_type ("latency", "slow_tail") when they fire.  These are
    additive events and do not replace the terminating outcome event.
  - The terminating outcome (http_error, malformed, etc.) records one event
    per request via service.record_injection().
"""

from __future__ import annotations

import asyncio
import random
import ssl
import socket
import uuid
from typing import AsyncIterator, Callable, Coroutine, Optional, Any, cast

import httpx

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
    select_outcome,
)
from code_indexer.server.fault_injection.fault_profile import (
    FaultProfile,
    jitter_uniform,
    roll_bernoulli,
)
from code_indexer.server.fault_injection._transport_helpers import (
    build_error_response,
    build_corrupted_json_response,
    build_redirect_302,
)

# ---------------------------------------------------------------------------
# Outcome classification sets
# ---------------------------------------------------------------------------

# All outcomes that raise an exception immediately — latency injection is
# skipped for these because applying a sleep before raising is misleading
# and contradicts the expected failure semantics.
_IMMEDIATE_RAISE_OUTCOMES = frozenset(
    {
        "connect_timeout",
        "read_timeout",
        "write_timeout",
        "pool_timeout",
        "connect_error",
        "dns_failure",
        "tls_error",
    }
)

# The outcome that delegates to the wrapped transport with no event recorded
_PASS_THROUGH = "pass_through"


def build_truncating_stream_response(
    body: bytes,
    truncate_at: int,
    request: httpx.Request,
) -> httpx.Response:
    """
    Return a real httpx.Response whose async byte stream yields exactly
    *truncate_at* bytes then raises httpx.StreamError (Scenario 31).
    """
    stream = _TruncatingStream(body=body, truncate_at=truncate_at)
    return httpx.Response(
        status_code=200,
        stream=stream,
        request=request,
    )


class _TruncatingStream(httpx.AsyncByteStream):
    """
    Async byte stream that yields exactly *truncate_at* bytes then raises
    httpx.StreamError, simulating a mid-stream connection reset.
    """

    def __init__(self, body: bytes, truncate_at: int) -> None:
        self._body = body
        self._truncate_at = truncate_at

    async def __aiter__(self) -> AsyncIterator[bytes]:
        payload = self._body[: self._truncate_at]
        if payload:
            yield payload
        raise httpx.StreamError("fault-injected stream disconnect")


# ---------------------------------------------------------------------------
# FaultInjectingTransport
# ---------------------------------------------------------------------------


class FaultInjectingTransport(httpx.AsyncBaseTransport):
    """
    Wraps another AsyncBaseTransport and injects faults on matching requests.

    Parameters
    ----------
    wrapped:
        The real transport (or stub in tests) to delegate pass-through and
        stream_disconnect requests to.
    service:
        The FaultInjectionService that provides profile lookup, RNG, and
        event recording.
    sleep_fn:
        An async callable with signature ``async (delay_seconds: float) ->
        None``.  Defaults to ``asyncio.sleep``.  Inject a capturing or
        cancelling callable in tests to avoid patching asyncio directly.
    """

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        service: FaultInjectionService,
        sleep_fn: Optional[Callable[[float], Coroutine[Any, Any, None]]] = None,
    ) -> None:
        self._wrapped = wrapped
        self._service = service
        self._sleep_fn = sleep_fn if sleep_fn is not None else asyncio.sleep

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        profile = self._service.match_profile_snapshot(str(request.url))
        if profile is None:
            return await self._wrapped.handle_async_request(request)

        # Per-request isolated RNG (M4): seed drawn atomically under lock so that
        # concurrent set_seed() calls cannot corrupt in-flight outcome selection.
        rng = random.Random(self._service.draw_per_request_seed())
        correlation_id = str(uuid.uuid4())
        outcome = select_outcome(profile, rng)

        # Additive latency — skipped for all immediate-raise outcomes
        if outcome not in _IMMEDIATE_RAISE_OUTCOMES:
            await self._apply_additive_latency(profile, rng, correlation_id)

        if outcome == _PASS_THROUGH:
            return await self._wrapped.handle_async_request(request)

        # Record the terminating outcome event (one per request)
        self._service.record_injection(profile.target, outcome, correlation_id)

        return await self._dispatch(outcome, profile, rng, request, correlation_id)

    async def _sleep_with_cancel_record(
        self,
        delay_seconds: float,
        target: str,
        correlation_id: str,
    ) -> None:
        """Sleep for *delay_seconds*, recording a 'cancelled' event on CancelledError.

        Shared by latency and slow_tail branches to avoid code duplication.
        CancelledError is always re-raised after the event is recorded (C2/Scenario 17).
        """
        try:
            await self._sleep_fn(delay_seconds)
        except asyncio.CancelledError:
            self._service.record_injection(target, "cancelled", correlation_id)
            raise

    async def _apply_additive_latency(
        self,
        profile: FaultProfile,
        rng: random.Random,
        correlation_id: str,
    ) -> None:
        """
        Apply base latency and slow-tail latency when their Bernoulli rolls fire.

        Each additive event is recorded separately with fault_type "latency"
        or "slow_tail" — these are distinct from the terminating outcome event.
        CancelledError from sleep_fn propagates unconditionally after recording
        a 'cancelled' event (C2/Scenario 17).
        """
        if roll_bernoulli(profile.latency_rate, rng):
            delay_ms = jitter_uniform(*profile.latency_ms_range, rng)
            self._service.record_injection(profile.target, "latency", correlation_id)
            await self._sleep_with_cancel_record(
                delay_ms / 1000.0, profile.target, correlation_id
            )

        if roll_bernoulli(profile.slow_tail_rate, rng):
            delay_ms = jitter_uniform(*profile.slow_tail_ms_range, rng)
            self._service.record_injection(profile.target, "slow_tail", correlation_id)
            await self._sleep_with_cancel_record(
                delay_ms / 1000.0, profile.target, correlation_id
            )

    async def _dispatch(
        self,
        outcome: str,
        profile: FaultProfile,
        rng: random.Random,
        request: httpx.Request,
        correlation_id: str,
    ) -> httpx.Response:
        if outcome == "http_error":
            # cast: _transport_helpers functions return httpx.Response at runtime;
            # mypy infers Any due to cross-module import with from __future__ import annotations.
            return cast(httpx.Response, build_error_response(profile, rng, request))

        if outcome == "malformed_json":
            # cast: same reason as http_error above.
            return cast(
                httpx.Response, build_corrupted_json_response(profile, rng, request)
            )

        if outcome == "redirect_loop":
            # cast: same reason as http_error above.
            return cast(httpx.Response, build_redirect_302(request))

        if outcome == "stream_disconnect":
            real_response = await self._wrapped.handle_async_request(request)
            try:
                body = await real_response.aread()
            finally:
                await real_response.aclose()
            truncate_at = int(jitter_uniform(*profile.truncate_after_bytes_range, rng))
            return build_truncating_stream_response(body, truncate_at, request)

        if outcome == "connect_timeout":
            raise httpx.ConnectTimeout("fault-injected", request=request)

        if outcome == "read_timeout":
            raise httpx.ReadTimeout("fault-injected", request=request)

        if outcome == "write_timeout":
            raise httpx.WriteTimeout("fault-injected", request=request)

        if outcome == "pool_timeout":
            raise httpx.PoolTimeout("fault-injected", request=request)

        if outcome == "connect_error":
            raise httpx.ConnectError("fault-injected", request=request)

        if outcome == "dns_failure":
            cause = socket.gaierror("fault-injected DNS failure")
            raise httpx.ConnectError(
                "fault-injected DNS failure", request=request
            ) from cause

        if outcome == "tls_error":
            tls_cause = ssl.SSLError("fault-injected TLS error")
            raise httpx.ConnectError(
                "fault-injected TLS error", request=request
            ) from tls_cause

        # Unreachable: select_outcome returns only known outcomes or pass_through
        raise RuntimeError(f"Unhandled fault outcome: {outcome!r}")  # pragma: no cover
