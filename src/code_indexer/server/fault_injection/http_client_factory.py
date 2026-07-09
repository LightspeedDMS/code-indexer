"""
HttpClientFactory — single enforced construction path for all outbound
HTTP clients (async and sync) in the CIDX server.

Story #746 — Phase C wiring.

Every call to httpx.AsyncClient() or httpx.Client() in src/code_indexer/server/
or in the CLI-facing provider services (voyage_ai.py, cohere_embedding.py) that
makes outbound calls to external providers MUST go through this factory.  When
the fault injection harness is active, the factory wraps the default transport
with FaultInjectingTransport (async) or FaultInjectingSyncTransport (sync) so
that injection profiles are transparently applied to all outbound requests.

Usage (async):
    factory = HttpClientFactory(fault_injection_service=svc)
    async with factory.create_client(timeout=30.0) as client:
        response = await client.post(url, json=payload)

Usage (sync):
    with factory.create_sync_client(transport=latency_transport, timeout=30.0) as client:
        response = client.post(url, json=payload)

When fault injection is disabled (service is None or service.enabled is False),
the returned clients behave identically to plain httpx.AsyncClient() /
httpx.Client() respectively.

Story #1083 — production connection pooling (sync path):
    Callers on the production hot path pass ``pooled=True`` to
    ``create_sync_client``.  When fault injection is OFF, the factory owns ONE
    long-lived keep-alive httpx.Client (reused SSLContext + connection pool, built
    lazily once) and returns it wrapped in ``_BorrowedClientContext`` whose
    ``__exit__`` is a no-op — the provider borrows the shared client for one
    request and returns it without closing it.  The pooled client is closed once
    at lifespan shutdown via ``close_pooled_clients()``.  Auth headers travel on
    the per-request ``.post()`` call, so the pooled client is auth-agnostic.

    When fault injection is ACTIVE, ``pooled`` is IGNORED: the factory returns a
    fresh per-call client wrapped in FaultInjectingSyncTransport (closed per call)
    so every scripted fault still intercepts every call exactly as before.

Story #1083 residual — bake the latency transport in ONCE on the pooled path:
    The factory OWNS latency-transport construction.  Providers no longer build a
    fresh ``build_latency_transport()`` (a new ``httpx.HTTPTransport`` + its
    ``SSLContext``) per embed call.  Instead:

    * Pooled production path (fault OFF): the factory calls
      ``build_latency_transport()`` exactly ONCE, at pooled-client construction,
      and bakes the single shared LatencyTrackingHTTPXTransport into the
      keep-alive client.  Every subsequent pooled request reuses it — killing the
      per-query SSLContext/transport churn.  ``build_latency_transport()`` is a
      thin timing wrapper with no per-request mutable state (``_wrapped`` /
      ``_tracker`` / ``_registry`` are read-only after construction), so one
      shared instance is safe across concurrent requests and still records a
      latency sample per request via the DependencyLatencyTracker.

    * Fault-injection path (fault ON): the factory builds the latency transport
      per call (fresh per-call client) and wraps it in FaultInjectingSyncTransport
      — UNCHANGED composition ``FaultInjectingSyncTransport →
      LatencyTrackingHTTPXTransport → HTTPTransport``.

    A caller-supplied ``transport`` always wins over the internal build (used by
    tests and any caller that composes its own transport).
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Any, Optional, Type

import httpx

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_injecting_transport import (
    FaultInjectingTransport,
)
from code_indexer.server.fault_injection.fault_injecting_sync_transport import (
    FaultInjectingSyncTransport,
)
from code_indexer.server.services.latency_tracking_httpx_transport import (
    build_latency_transport,
)

# Story #1083: connection-pool limits for the long-lived production sync client.
# Sized above the embedding governor's per-budget concurrency cap (K=16, Bug #1078)
# so concurrent query-embedding HTTP calls never starve on keep-alive slots, while
# still bounding the total open connections (MESSI #14 — bounded resources).
DEFAULT_MAX_KEEPALIVE_CONNECTIONS = 20
DEFAULT_MAX_CONNECTIONS = 40


class _BorrowedClientContext:
    """Context manager that lends a shared pooled client WITHOUT closing it.

    Story #1083: the production providers keep their ``with _client_ctx as client:``
    shape.  For the pooled keep-alive client, ``__exit__`` is a deliberate NO-OP so
    the provider borrows the shared client for the duration of one request and
    returns it — never tearing down the shared SSLContext / connection pool.  The
    pooled client is closed exactly once, at lifespan shutdown, via
    ``HttpClientFactory.close_pooled_clients()``.
    """

    __slots__ = ("_client",)

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def __enter__(self) -> httpx.Client:
        return self._client

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        # Intentional no-op: borrowing, not owning. Do NOT close the shared client.
        return None


class HttpClientFactory:
    """
    Factory for outbound HTTP clients (async and sync).

    Parameters
    ----------
    fault_injection_service:
        An active FaultInjectionService instance, or None.  When provided
        and the service is enabled, every client created by this factory
        will have a FaultInjectingTransport (async) or
        FaultInjectingSyncTransport (sync) installed.  When None or the
        service is disabled, clients are created with the default transport.
    """

    def __init__(
        self,
        fault_injection_service: Optional[FaultInjectionService] = None,
    ) -> None:
        self._fault_injection_service = fault_injection_service
        # Story #1083: ONE long-lived pooled sync client for the production path.
        # Built lazily on first pooled request, reused thereafter, closed at
        # lifespan shutdown.  Guarded by a lock so concurrent first-requests build
        # exactly one client (thread-safe single-flight).
        self._pooled_sync_client: Optional[httpx.Client] = None
        self._pool_lock = threading.Lock()

    def create_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """
        Create and return a new httpx.AsyncClient.

        All keyword arguments are forwarded to httpx.AsyncClient().  Callers
        should use this as an async context manager:

            async with factory.create_client(timeout=30.0) as client:
                ...

        If fault injection is active, the client's transport is wrapped with
        FaultInjectingTransport.  All other behaviour is identical to a
        standard httpx.AsyncClient.
        """
        svc = self._fault_injection_service
        if svc is not None and svc.enabled:
            default_transport = httpx.AsyncHTTPTransport()
            fault_transport = FaultInjectingTransport(
                wrapped=default_transport,
                service=svc,
            )
            return httpx.AsyncClient(transport=fault_transport, **kwargs)

        return httpx.AsyncClient(**kwargs)

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        pooled: bool = False,
        **kwargs: Any,
    ) -> Any:
        """
        Create and return a new sync httpx.Client.

        When fault injection is active, wraps the provided (or default) transport
        with FaultInjectingSyncTransport so outbound calls are subject to injection
        profiles.

        Callers that already build a LatencyTrackingHTTPXTransport should pass it
        as ``transport`` so the composition is:
            FaultInjectingSyncTransport → LatencyTrackingHTTPXTransport → HTTPTransport

        Composition order: FaultInjectingSyncTransport → LatencyTrackingHTTPXTransport →
        HTTPTransport.  Latency metrics reflect total wire time including any injected
        latency; however, the fault-injection counters, logs, and /admin/fault-injection/
        history ring buffer are the source of truth for injected behavior.  Do not
        repurpose production latency telemetry to measure synthetic fault characteristics.

        When fault injection is disabled, a plain httpx.Client is returned — with
        the caller-supplied transport if provided, otherwise the httpx default.

        Args:
            transport: Optional base transport to wrap.  If None and fault injection
                       is active, a bare httpx.HTTPTransport() is used as the base.
            **kwargs:  Additional keyword arguments forwarded to httpx.Client().

        Usage:
            with factory.create_sync_client(transport=latency_transport, timeout=30.0) as c:
                response = c.post(url, json=payload)
        """
        svc = self._fault_injection_service
        if svc is not None and svc.enabled:
            # Fault-injection ACTIVE (nonprod): UNCHANGED per-call fresh client +
            # fault transport, closed per call.  The ``pooled`` flag is ignored on
            # this path by design (Story #1083 approved compromise) so every
            # scripted fault still intercepts every call exactly as before.
            #
            # Story #1083 residual: the factory now OWNS latency-transport
            # construction.  On the fault path it builds one PER CALL (fresh
            # per-call client) so the composition stays
            # FaultInjectingSyncTransport → LatencyTrackingHTTPXTransport →
            # HTTPTransport.  A bare HTTPTransport is the floor when no tracker is
            # registered.
            base_transport: httpx.BaseTransport = self._resolve_transport(transport)
            fault_transport = FaultInjectingSyncTransport(
                wrapped=base_transport,
                service=svc,
            )
            return httpx.Client(transport=fault_transport, **kwargs)

        # Production path (fault injection OFF).
        if pooled:
            # Borrow the ONE long-lived keep-alive client (built once).  The
            # transport/kwargs from the FIRST pooled request are baked in; the
            # client is auth-agnostic (auth header travels on each .post()).
            client = self._get_or_create_pooled_client(transport=transport, **kwargs)
            return _BorrowedClientContext(client)

        if transport is not None:
            return httpx.Client(transport=transport, **kwargs)
        return httpx.Client(**kwargs)

    @staticmethod
    def _resolve_transport(
        transport: Optional[httpx.BaseTransport],
    ) -> httpx.BaseTransport:
        """Return a caller-supplied transport, else build the latency transport.

        Story #1083 residual: the factory owns latency-transport construction so
        providers stop building a fresh ``build_latency_transport()`` (and its
        SSLContext) per call.  A caller-supplied ``transport`` always wins.  When
        none is supplied, ``build_latency_transport()`` is consulted; if it returns
        None (no DependencyLatencyTracker registered — e.g. CLI / tests), a bare
        ``httpx.HTTPTransport`` is the floor so the returned client is always
        wired with a concrete transport.
        """
        if transport is not None:
            return transport
        latency: Optional[httpx.BaseTransport] = build_latency_transport()
        if latency is not None:
            return latency
        return httpx.HTTPTransport()

    def _get_or_create_pooled_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        **kwargs: Any,
    ) -> httpx.Client:
        """Return the long-lived pooled production client, building it once.

        Thread-safe single-flight: the first caller builds the keep-alive client
        under ``_pool_lock`` with bounded ``httpx.Limits``; all later callers reuse
        it.  ``httpx.Client`` is safe for concurrent requests across threads, which
        is exactly how the shared client is exercised.

        Story #1083 residual: the single shared latency transport is built HERE,
        exactly once, under the lock — so ``build_latency_transport()`` (and its
        ``SSLContext`` / ``httpx.HTTPTransport``) is constructed once per pool, not
        once per query.  The latency transport is a thin timing wrapper with no
        per-request mutable state, so the one shared instance safely records a
        sample for every concurrent request.
        """
        client = self._pooled_sync_client
        if client is not None:
            return client
        with self._pool_lock:
            if self._pooled_sync_client is not None:
                return self._pooled_sync_client
            limits = httpx.Limits(
                max_keepalive_connections=DEFAULT_MAX_KEEPALIVE_CONNECTIONS,
                max_connections=DEFAULT_MAX_CONNECTIONS,
            )
            # Build the latency transport ONCE and bake it into the pooled client.
            baked_transport = self._resolve_transport(transport)
            built = httpx.Client(transport=baked_transport, limits=limits, **kwargs)
            self._pooled_sync_client = built
            return built

    def close_pooled_clients(self) -> None:
        """Close the long-lived pooled production client (lifespan shutdown).

        Idempotent.  After close the reference is dropped so a subsequent pooled
        request rebuilds a fresh keep-alive client.  No-op when no pooled client
        was ever built.
        """
        with self._pool_lock:
            client = self._pooled_sync_client
            self._pooled_sync_client = None
        if client is not None:
            client.close()
