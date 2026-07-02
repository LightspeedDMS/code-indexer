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
"""

from __future__ import annotations

from typing import Any, Optional

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
        **kwargs: Any,
    ) -> httpx.Client:
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
            base_transport: httpx.BaseTransport = (
                transport if transport is not None else httpx.HTTPTransport()
            )
            fault_transport = FaultInjectingSyncTransport(
                wrapped=base_transport,
                service=svc,
            )
            return httpx.Client(transport=fault_transport, **kwargs)

        if transport is not None:
            return httpx.Client(transport=transport, **kwargs)
        return httpx.Client(**kwargs)
