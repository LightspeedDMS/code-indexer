"""Story #1083 residual: bake the latency transport into the pooled client ONCE.

The #1083 pooled production client reuses the TCP/TLS connection, but the
``SSLContext``/``httpx.HTTPTransport`` construction did NOT drop because the
providers still called ``build_latency_transport()`` on EVERY embed call (which
constructs a fresh ``httpx.HTTPTransport`` and its ``SSLContext`` per call) and
the pooled factory then DISCARDED that per-call transport.

This module proves the residual is closed:

  - On the pooled production path (fault injection OFF), the factory builds the
    latency transport exactly ONCE (baked into the shared keep-alive client) no
    matter how many pooled requests flow through it.
  - On the fault-injection path the latency transport is still built per call
    (fresh per-call client, wrapped in ``FaultInjectingSyncTransport``).
  - Latency tracking still records on the pooled path (the single shared latency
    transport times every request and feeds the DependencyLatencyTracker).
  - The Voyage and Cohere providers no longer construct a per-call latency
    transport on the pooled path — the factory owns that construction.

All tests are unit-level; no real providers are called.
"""

from __future__ import annotations

import os
import random
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.server.fault_injection import http_client_factory as factory_mod
from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_injecting_sync_transport import (
    FaultInjectingSyncTransport,
)
from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services import dependency_latency_tracker as tracker_mod
from code_indexer.server.services.latency_tracking_httpx_transport import (
    LatencyTrackingHTTPXTransport,
)

_SEED = 42


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(_SEED))


class _RecordingTracker:
    """Minimal DependencyLatencyTracker stand-in that records samples."""

    def __init__(self) -> None:
        self.samples: List[tuple] = []

    def record_sample(
        self, dependency_name: str, latency_ms: float, status_code: int
    ) -> None:
        self.samples.append((dependency_name, latency_ms, status_code))


class _StubWrappedTransport(httpx.BaseTransport):
    """Returns a canned 200 for any request — stands in for the wire transport."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)


# ===========================================================================
# Build-once on the pooled production path
# ===========================================================================


class TestPooledLatencyTransportBuiltOnce:
    def test_factory_builds_latency_transport_once_across_pooled_calls(self) -> None:
        """N>1 pooled requests must construct the latency transport exactly ONCE."""
        factory = HttpClientFactory(fault_injection_service=None)

        sentinel_transport = httpx.HTTPTransport()
        call_count = {"n": 0}

        def _fake_build() -> httpx.BaseTransport:
            call_count["n"] += 1
            return sentinel_transport

        try:
            with patch.object(
                factory_mod, "build_latency_transport", side_effect=_fake_build
            ):
                clients = []
                for _ in range(5):
                    with factory.create_sync_client(pooled=True) as c:
                        clients.append(c)
            # Built ONCE, baked into the shared client, reused thereafter.
            assert call_count["n"] == 1, (
                f"Expected build_latency_transport called once on the pooled path, "
                f"got {call_count['n']}"
            )
            assert all(c is clients[0] for c in clients)
            assert clients[0]._transport is sentinel_transport
        finally:
            factory.close_pooled_clients()

    def test_explicit_transport_still_honoured_over_internal_build(self) -> None:
        """An explicitly supplied transport wins and suppresses the internal build."""
        factory = HttpClientFactory(fault_injection_service=None)
        explicit = httpx.HTTPTransport()
        try:
            with patch.object(factory_mod, "build_latency_transport") as build_spy:
                with factory.create_sync_client(
                    pooled=True, transport=explicit
                ) as client:
                    assert client._transport is explicit
                build_spy.assert_not_called()
        finally:
            factory.close_pooled_clients()


# ===========================================================================
# Fault-injection path still builds per call
# ===========================================================================


class TestFaultPathBuildsLatencyTransportPerCall:
    def test_fault_path_builds_latency_transport_each_call(self) -> None:
        """Fault injection ON: latency transport built per call (fresh client)."""
        factory = HttpClientFactory(fault_injection_service=_make_service())
        call_count = {"n": 0}

        def _fake_build() -> httpx.BaseTransport:
            call_count["n"] += 1
            return httpx.HTTPTransport()

        with patch.object(
            factory_mod, "build_latency_transport", side_effect=_fake_build
        ):
            for _ in range(4):
                with factory.create_sync_client(pooled=True) as client:
                    assert isinstance(client._transport, FaultInjectingSyncTransport)
        assert call_count["n"] == 4, (
            f"Fault path must build the latency transport per call, got {call_count['n']}"
        )

    def test_fault_path_composition_latency_under_fault(self) -> None:
        """Composition: FaultInjectingSyncTransport -> LatencyTrackingHTTPXTransport."""
        tracker = _RecordingTracker()
        tracker_mod.set_instance(tracker)  # type: ignore[arg-type]
        try:
            factory = HttpClientFactory(fault_injection_service=_make_service())
            with factory.create_sync_client(pooled=True) as client:
                assert isinstance(client._transport, FaultInjectingSyncTransport)
                inner = client._transport._wrapped
                assert isinstance(inner, LatencyTrackingHTTPXTransport)
        finally:
            tracker_mod.set_instance(None)


# ===========================================================================
# Latency tracking still records on the pooled path
# ===========================================================================


class TestPooledLatencyTrackingStillRecords:
    def test_pooled_client_records_latency_sample(self) -> None:
        """The single baked-in latency transport must still record per-request."""
        tracker = _RecordingTracker()
        tracker_mod.set_instance(tracker)  # type: ignore[arg-type]
        stub = _StubWrappedTransport()

        # Force build_latency_transport to wrap our stub wire transport so we can
        # drive a request without touching the network, while keeping the real
        # LatencyTrackingHTTPXTransport timing/recording logic intact.
        def _build_with_stub() -> LatencyTrackingHTTPXTransport:
            from code_indexer.server.services.latency_tracking_httpx_transport import (
                DependencyRegistryBuilder,
                DEFAULT_REGISTRY_ENTRIES,
            )

            reg = DependencyRegistryBuilder()
            for host, prefix, dep in DEFAULT_REGISTRY_ENTRIES:
                reg.register(host, prefix, dep)
            return LatencyTrackingHTTPXTransport(
                wrapped_transport=stub,  # type: ignore[arg-type]
                tracker=tracker,
                registry=reg.build(),
            )

        factory = HttpClientFactory(fault_injection_service=None)
        try:
            with patch.object(
                factory_mod, "build_latency_transport", side_effect=_build_with_stub
            ):
                # Two pooled requests through the SAME shared client.
                for _ in range(2):
                    with factory.create_sync_client(pooled=True) as client:
                        client.post(
                            "https://api.voyageai.com/v1/embeddings", json={"x": 1}
                        )
        finally:
            factory.close_pooled_clients()
            tracker_mod.set_instance(None)

        assert len(tracker.samples) == 2, (
            f"Latency tracker must record one sample per pooled request, "
            f"got {tracker.samples!r}"
        )
        assert all(s[0] == "voyageai_embed" for s in tracker.samples)
        assert all(s[2] == 200 for s in tracker.samples)


# ===========================================================================
# Providers no longer build the latency transport on the pooled path
# ===========================================================================

_GOOD_VOYAGE: Dict[str, Any] = {
    "data": [{"embedding": [0.1] * 1024}],
    "usage": {"total_tokens": 5},
}
_GOOD_COHERE: Dict[str, Any] = {
    "embeddings": {"float": [[0.1] * 1024]},
    "meta": {"billed_units": {"input_tokens": 5}},
}


class _PostOnlyClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class _VoyageClient(_PostOnlyClient):
    def post(self, url, **kwargs):
        req = httpx.Request("POST", url)
        return httpx.Response(200, json=_GOOD_VOYAGE, request=req)


class _CohereClient(_PostOnlyClient):
    def post(self, url, **kwargs):
        req = httpx.Request("POST", url)
        return httpx.Response(200, json=_GOOD_COHERE, request=req)


class _StubFactory:
    """Records pooled flag + transport, returns a canned client."""

    def __init__(self, client) -> None:
        self._client = client
        self.calls: List[dict] = []

    def create_sync_client(self, **kwargs: Any):
        self.calls.append(kwargs)
        return self._client


class TestProvidersDoNotBuildLatencyTransportOnPooledPath:
    def test_voyage_does_not_build_latency_transport(self) -> None:
        from src.code_indexer.config import VoyageAIConfig
        from src.code_indexer.services import voyage_ai as voyage_mod
        from src.code_indexer.services.voyage_ai import VoyageAIClient

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "k-1083"}):
            client = VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))
        client._http_client_factory = _StubFactory(_VoyageClient())  # type: ignore[assignment]

        # If the provider still references build_latency_transport, this patch
        # would record calls. The provider must NOT call it on the pooled path.
        if hasattr(voyage_mod, "build_latency_transport"):
            with patch.object(voyage_mod, "build_latency_transport") as spy:
                client._make_sync_request(["hello"], retry=False)
                spy.assert_not_called()
        else:
            # Symbol removed from the provider module entirely — strongest proof.
            client._make_sync_request(["hello"], retry=False)

        # And the provider must NOT pass a transport on the pooled call.
        factory: _StubFactory = client._http_client_factory  # type: ignore[assignment]
        assert factory.calls[0].get("pooled") is True
        assert factory.calls[0].get("transport") is None, (
            "Provider must not pass a per-call latency transport on the pooled path"
        )

    def test_cohere_does_not_build_latency_transport(self) -> None:
        from src.code_indexer.config import CohereConfig
        from src.code_indexer.services import cohere_embedding as cohere_mod
        from src.code_indexer.services.cohere_embedding import CohereEmbeddingProvider

        with patch.dict(os.environ, {"CO_API_KEY": "k-1083"}):
            provider = CohereEmbeddingProvider(CohereConfig(model="embed-v4.0"))
        provider._http_client_factory = _StubFactory(_CohereClient())  # type: ignore[assignment]

        if hasattr(cohere_mod, "build_latency_transport"):
            with patch.object(cohere_mod, "build_latency_transport") as spy:
                provider._make_sync_request(["hello"], retry=False)
                spy.assert_not_called()
        else:
            provider._make_sync_request(["hello"], retry=False)

        factory: _StubFactory = provider._http_client_factory  # type: ignore[assignment]
        assert factory.calls[0].get("pooled") is True
        assert factory.calls[0].get("transport") is None, (
            "Provider must not pass a per-call latency transport on the pooled path"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
