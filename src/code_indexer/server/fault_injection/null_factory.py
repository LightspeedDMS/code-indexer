"""
NullFaultFactory — passthrough factory that never installs fault-injection transports.

Story #746 — CRITICAL fix: eliminate silent fallback pattern.

Use this wherever a concrete HttpClientFactory is not available (CLI-only
deployments, test fixtures that do not need fault injection).  Eliminates the
need for ``if factory is None`` fallback branches in provider call sites, which
would silently bypass fault injection and violate MESSI Rule 2 (anti-fallback).

Every provider MUST be constructed with a factory.  If fault injection is
genuinely not needed, pass NullFaultFactory() — the constructor call is
explicit and visible, unlike a None default that silently degrades.

NullFaultFactory inherits from HttpClientFactory so it is type-compatible
wherever HttpClientFactory is expected (e.g. server-layer reranker clients).
It also satisfies the SyncClientFactory Protocol used in CLI-layer providers
(voyage_ai.py, cohere_embedding.py) because it exposes create_sync_client().
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory


class NullFaultFactory(HttpClientFactory):
    """Passthrough factory that always returns plain httpx clients.

    Subclasses HttpClientFactory for type-safety at server-layer call sites.
    Also satisfies the SyncClientFactory Protocol used in CLI-layer providers.

    create_sync_client() honours the ``transport`` kwarg so that callers that
    compose their own LatencyTrackingHTTPXTransport receive a client wired with
    that transport — the same behaviour as HttpClientFactory when fault injection
    is disabled.
    """

    def __init__(self) -> None:
        """No fault injection service needed — always passthrough."""
        super().__init__(fault_injection_service=None)

    def create_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """Return a plain httpx.AsyncClient with no fault-injection transport."""
        return httpx.AsyncClient(**kwargs)

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        **kwargs: Any,
    ) -> httpx.Client:
        """Return a plain httpx.Client, honouring any caller-supplied transport."""
        if transport is not None:
            return httpx.Client(transport=transport, **kwargs)
        return httpx.Client(**kwargs)
