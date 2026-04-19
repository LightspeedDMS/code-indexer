"""
FaultInjectingSyncTransport — httpx.BaseTransport subclass that intercepts
outbound synchronous requests and applies configurable fault modes per target.

Story #746 — Phase C architectural fix (C1 gap).

Mirrors FaultInjectingTransport (async) for the sync call paths used by:
  - VoyageAIClient._make_sync_request (voyage_ai.py)
  - VoyageAIClient._health_probe (voyage_ai.py)
  - CohereEmbeddingProvider._make_request (cohere_embedding.py)
  - CohereEmbeddingProvider._health_probe (cohere_embedding.py)
  - VoyageRerankerClient._post (reranker_clients.py)
  - CohereRerankerClient._post (reranker_clients.py)

Design notes:
  - All randomness goes through service.rng (injectable for determinism).
  - Latency / slow_tail use time.sleep (sync). CancelledError behaviour from
    asyncio.sleep does NOT apply here — sync sleep is not cancellable.
    Scenario 17 (cancellation recording) applies to the async transport only.
    This is documented intentionally; do not add asyncio.CancelledError handling
    here as it will never fire.
  - All exception types (ConnectTimeout, ReadTimeout, etc.) are identical to the
    async transport — httpx exceptions are not async-specific.
  - pass_through delegates to self._wrapped.handle_request(request) (sync call).
  - Outcome-selection, jitter, and response-building are shared with the async
    transport via the same fault_injecting_transport helpers.
  - close() delegates to self._wrapped.close() to release connection pools and
    sockets owned by the wrapped transport.
  - stream_disconnect path reads and closes the real response in a try/finally
    to guarantee connection pool resources are released even on read errors.
"""

from __future__ import annotations

import random
import socket
import ssl
import time
import uuid
from typing import Iterator, cast

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
from code_indexer.server.fault_injection.fault_injecting_transport import (
    _IMMEDIATE_RAISE_OUTCOMES,
    _PASS_THROUGH,
)
from code_indexer.server.fault_injection._transport_helpers import (
    build_error_response,
    build_corrupted_json_response,
    build_redirect_302,
)


class _SyncTruncatingStream(httpx.SyncByteStream):
    """
    Sync byte stream that yields exactly *truncate_at* bytes then raises
    httpx.StreamError, simulating a mid-stream connection reset (sync path).
    """

    def __init__(self, body: bytes, truncate_at: int) -> None:
        self._body = body
        self._truncate_at = truncate_at

    def __iter__(self) -> Iterator[bytes]:
        payload = self._body[: self._truncate_at]
        if payload:
            yield payload
        raise httpx.StreamError("fault-injected stream disconnect")


def build_sync_truncating_stream_response(
    body: bytes,
    truncate_at: int,
    request: httpx.Request,
) -> httpx.Response:
    """
    Return a real httpx.Response whose sync byte stream yields exactly
    *truncate_at* bytes then raises httpx.StreamError (Scenario 31 — sync path).

    Raises ValueError if truncate_at is negative.
    """
    if truncate_at < 0:
        raise ValueError(f"truncate_at must be >= 0, got {truncate_at}")
    stream = _SyncTruncatingStream(body=body, truncate_at=truncate_at)
    return httpx.Response(
        status_code=200,
        stream=stream,
        request=request,
    )


class FaultInjectingSyncTransport(httpx.BaseTransport):
    """
    Wraps another BaseTransport and injects faults on matching requests.

    Parameters
    ----------
    wrapped:
        The real sync transport (or stub in tests) to delegate pass-through
        requests to.
    service:
        The FaultInjectionService that provides profile lookup, RNG, and
        event recording.

    Note on cancellation:
        sync time.sleep() is not cancellable via asyncio.CancelledError.
        Scenario 17 (cancelled event recording) applies only to the async
        FaultInjectingTransport.  This transport does not and cannot record
        cancellation events.
    """

    def __init__(
        self,
        wrapped: httpx.BaseTransport,
        service: FaultInjectionService,
    ) -> None:
        self._wrapped = wrapped
        self._service = service

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        profile = self._service.match_profile_snapshot(str(request.url))
        if profile is None:
            return self._wrapped.handle_request(request)

        # Per-request isolated RNG: seed drawn atomically so that concurrent
        # set_seed() calls cannot corrupt in-flight outcome selection (M4).
        rng = random.Random(self._service.draw_per_request_seed())
        correlation_id = str(uuid.uuid4())
        outcome = select_outcome(profile, rng)

        # Additive latency — skipped for all immediate-raise outcomes
        if outcome not in _IMMEDIATE_RAISE_OUTCOMES:
            self._apply_additive_latency(profile, rng, correlation_id)

        if outcome == _PASS_THROUGH:
            return self._wrapped.handle_request(request)

        # Record the terminating outcome event (one per request)
        self._service.record_injection(profile.target, outcome, correlation_id)

        return self._dispatch(outcome, profile, rng, request)

    def close(self) -> None:
        """Delegate close to the wrapped transport to release connection pools."""
        self._wrapped.close()

    def _apply_additive_latency(
        self,
        profile: FaultProfile,
        rng: random.Random,
        correlation_id: str,
    ) -> None:
        """
        Apply base latency and slow-tail latency when their Bernoulli rolls fire.

        Uses time.sleep (sync).  Each additive event is recorded separately with
        fault_type "latency" or "slow_tail".
        """
        if roll_bernoulli(profile.latency_rate, rng):
            delay_ms = jitter_uniform(*profile.latency_ms_range, rng)
            self._service.record_injection(profile.target, "latency", correlation_id)
            time.sleep(delay_ms / 1000.0)

        if roll_bernoulli(profile.slow_tail_rate, rng):
            delay_ms = jitter_uniform(*profile.slow_tail_ms_range, rng)
            self._service.record_injection(profile.target, "slow_tail", correlation_id)
            time.sleep(delay_ms / 1000.0)

    def _dispatch(
        self,
        outcome: str,
        profile: FaultProfile,
        rng: random.Random,
        request: httpx.Request,
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
            real_response = self._wrapped.handle_request(request)
            try:
                body = real_response.read()
            finally:
                real_response.close()
            truncate_at = int(jitter_uniform(*profile.truncate_after_bytes_range, rng))
            return build_sync_truncating_stream_response(body, truncate_at, request)

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
