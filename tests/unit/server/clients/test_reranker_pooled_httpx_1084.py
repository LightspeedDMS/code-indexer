"""Story #1084: extend Story #1083 production httpx pooling to the RERANKER lanes.

Story #1083 pooled the EMBEDDING providers' sync httpx client (production,
fault-injection OFF) via ``HttpClientFactory`` — the factory owns ONE keep-alive
``httpx.Client`` with the latency transport baked in ONCE; providers borrow it
(no-op close) and carry auth on the per-request ``.post()``.  The reranker
clients were NOT covered: ``VoyageRerankerClient._post`` and
``CohereRerankerClient._post`` each built a fresh ``build_latency_transport()``
and called ``create_sync_client(transport=..., timeout=...)`` WITHOUT
``pooled=True`` — a full TLS handshake / SSLContext build per rerank request.

This module proves the residual is closed for the ``:rerank`` lanes, mirroring
``test_pooled_latency_transport_build_once_1083.py``:

  - Pooled production path (fault OFF): the reranker reuses the factory's single
    pooled client across N>1 rerank calls; the latency transport is built ONCE.
  - The reranker passes ``pooled=True`` and NO per-call ``transport=`` to the
    factory, and never calls ``build_latency_transport()`` itself.
  - The reranker does NOT close the borrowed shared pooled client (borrow, not own).
  - Fault-injection ON: a fresh per-call fault-intercepted client is returned
    (unchanged), and ``pooled`` is ignored.
  - Auth header travels on every per-request ``.post()`` so key rotation is
    transparent (the pooled client stays auth-agnostic).

All tests are unit-level; no real reranker provider is called.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

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

_SEED = 42

_GOOD_VOYAGE: Dict[str, Any] = {"data": [{"index": 0, "relevance_score": 0.9}]}
_GOOD_COHERE: Dict[str, Any] = {"results": [{"index": 0, "relevance_score": 0.9}]}


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(_SEED))


def _make_config_service(api_key: str = "k-1084") -> MagicMock:
    """Return a MagicMock config service exposing the given api key for both providers."""
    mock_config = MagicMock()
    mock_config.claude_integration_config.voyageai_api_key = api_key
    mock_config.claude_integration_config.cohere_api_key = api_key
    mock_config.rerank_config.voyage_reranker_model = None
    mock_config.rerank_config.cohere_reranker_model = None
    mock_cs = MagicMock()
    mock_cs.get_config.return_value = mock_config
    return mock_cs


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Captures every .post() call and records whether close() was invoked."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.post_calls: List[dict] = []
        self.closed = False

    def __enter__(self) -> "_RecordingClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.post_calls.append(kwargs)
        req = httpx.Request("POST", url)
        return httpx.Response(200, json=self._payload, request=req)


class _StubWireTransport(httpx.BaseTransport):
    """Canned-200 wire transport so the pooled client can be exercised offline."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_GOOD_VOYAGE, request=request)


class _StubFactory:
    """Records create_sync_client kwargs and returns a single shared client.

    Mirrors the #1083 build-once test's _StubFactory: the SAME client object is
    handed back on every call, so a test can assert the reranker reuses (borrows)
    one pooled client and never closes it.
    """

    def __init__(self, client: _RecordingClient) -> None:
        self._client = client
        self.calls: List[dict] = []

    def create_sync_client(self, **kwargs: Any) -> _RecordingClient:
        self.calls.append(kwargs)
        return self._client


def _patch_cs():
    return patch(
        "code_indexer.server.clients.reranker_clients.get_config_service",
        return_value=_make_config_service(),
    )


def _make_voyage(factory):
    from code_indexer.server.clients.reranker_clients import VoyageRerankerClient

    client = VoyageRerankerClient(http_client_factory=factory)  # type: ignore[arg-type]
    return client


def _make_cohere(factory):
    from code_indexer.server.clients.reranker_clients import CohereRerankerClient

    client = CohereRerankerClient(http_client_factory=factory)  # type: ignore[arg-type]
    return client


# ===========================================================================
# (a)/(b) Pooled path: pooled=True, no per-call transport, build-once
# ===========================================================================


class TestRerankerUsesPooledClient:
    def test_voyage_passes_pooled_true_and_no_transport(self) -> None:
        rec = _RecordingClient(_GOOD_VOYAGE)
        factory = _StubFactory(rec)
        client = _make_voyage(factory)
        with _patch_cs():
            client.rerank("q", ["doc-a"])
        assert factory.calls, "Voyage reranker must call create_sync_client"
        assert factory.calls[0].get("pooled") is True
        assert factory.calls[0].get("transport") is None, (
            "Voyage reranker must NOT pass a per-call latency transport on the pooled path"
        )

    def test_cohere_passes_pooled_true_and_no_transport(self) -> None:
        rec = _RecordingClient(_GOOD_COHERE)
        factory = _StubFactory(rec)
        client = _make_cohere(factory)
        with _patch_cs():
            client.rerank("q", ["doc-a"])
        assert factory.calls, "Cohere reranker must call create_sync_client"
        assert factory.calls[0].get("pooled") is True
        assert factory.calls[0].get("transport") is None, (
            "Cohere reranker must NOT pass a per-call latency transport on the pooled path"
        )

    def test_voyage_does_not_build_latency_transport(self) -> None:
        from code_indexer.server.clients import reranker_clients as mod

        rec = _RecordingClient(_GOOD_VOYAGE)
        client = _make_voyage(_StubFactory(rec))
        # The reranker module must NOT reference build_latency_transport at all
        # on the pooled path — the factory owns that construction.
        if hasattr(mod, "build_latency_transport"):
            with _patch_cs():
                with patch.object(mod, "build_latency_transport") as spy:
                    client.rerank("q", ["doc-a"])
                    spy.assert_not_called()
        else:
            with _patch_cs():
                client.rerank("q", ["doc-a"])

    def test_cohere_does_not_build_latency_transport(self) -> None:
        from code_indexer.server.clients import reranker_clients as mod

        rec = _RecordingClient(_GOOD_COHERE)
        client = _make_cohere(_StubFactory(rec))
        if hasattr(mod, "build_latency_transport"):
            with _patch_cs():
                with patch.object(mod, "build_latency_transport") as spy:
                    client.rerank("q", ["doc-a"])
                    spy.assert_not_called()
        else:
            with _patch_cs():
                client.rerank("q", ["doc-a"])

    def test_voyage_reuses_one_pooled_client_build_once(self) -> None:
        """5 real rerank() calls reuse the factory's single pooled client (build-once).

        Drives the reranker end-to-end through a REAL HttpClientFactory (fault OFF):
        build_latency_transport is patched to wrap a canned-200 stub wire transport,
        proving the reranker borrows ONE shared pooled httpx.Client across all calls
        and the latency transport is constructed exactly once (no per-query churn).
        """
        factory = HttpClientFactory(fault_injection_service=None)
        build_count = {"n": 0}

        def _fake_build() -> httpx.BaseTransport:
            build_count["n"] += 1
            return _StubWireTransport()

        reranker = _make_voyage(factory)
        seen_clients: List[httpx.Client] = []
        real_create = factory.create_sync_client

        def _spy_create(**kwargs: Any) -> Any:
            ctx = real_create(**kwargs)
            seen_clients.append(ctx._client)  # type: ignore[attr-defined]
            return ctx

        try:
            with _patch_cs():
                with patch.object(
                    factory_mod, "build_latency_transport", side_effect=_fake_build
                ):
                    with patch.object(
                        factory, "create_sync_client", side_effect=_spy_create
                    ):
                        for _ in range(5):
                            reranker.rerank("q", ["doc-a"])
            assert len(seen_clients) == 5
            assert all(c is seen_clients[0] for c in seen_clients), (
                "Reranker must borrow ONE shared pooled client across rerank calls"
            )
            assert build_count["n"] == 1, (
                f"latency transport must be built once on the pooled path, "
                f"got {build_count['n']}"
            )
        finally:
            factory.close_pooled_clients()


# ===========================================================================
# (c) Borrow, not own: reranker must NOT close the shared pooled client
# ===========================================================================


class TestRerankerDoesNotCloseSharedClient:
    def test_voyage_does_not_close_borrowed_client(self) -> None:
        rec = _RecordingClient(_GOOD_VOYAGE)
        client = _make_voyage(_StubFactory(rec))
        with _patch_cs():
            client.rerank("q", ["doc-a"])
            client.rerank("q", ["doc-b"])
        assert rec.closed is False, (
            "Voyage reranker must NOT close the shared borrowed pooled client"
        )
        assert len(rec.post_calls) == 2

    def test_cohere_does_not_close_borrowed_client(self) -> None:
        rec = _RecordingClient(_GOOD_COHERE)
        client = _make_cohere(_StubFactory(rec))
        with _patch_cs():
            client.rerank("q", ["doc-a"])
            client.rerank("q", ["doc-b"])
        assert rec.closed is False, (
            "Cohere reranker must NOT close the shared borrowed pooled client"
        )
        assert len(rec.post_calls) == 2


# ===========================================================================
# (d) Fault-injection ON: fresh per-call fault-intercepted client (unchanged)
# ===========================================================================


class TestRerankerFaultPathUnchanged:
    def test_voyage_fault_path_returns_fault_intercepted_client(self) -> None:
        factory = HttpClientFactory(fault_injection_service=_make_service())
        # On the fault path, create_sync_client(pooled=True) ignores pooled and
        # returns a fresh httpx.Client whose transport is FaultInjectingSyncTransport.
        for _ in range(3):
            with factory.create_sync_client(pooled=True) as c:
                assert isinstance(c._transport, FaultInjectingSyncTransport)

    def test_fault_path_builds_latency_transport_per_call(self) -> None:
        factory = HttpClientFactory(fault_injection_service=_make_service())
        call_count = {"n": 0}

        def _fake_build() -> httpx.BaseTransport:
            call_count["n"] += 1
            return httpx.HTTPTransport()

        with patch.object(
            factory_mod, "build_latency_transport", side_effect=_fake_build
        ):
            for _ in range(4):
                with factory.create_sync_client(pooled=True) as c:
                    assert isinstance(c._transport, FaultInjectingSyncTransport)
        assert call_count["n"] == 4


# ===========================================================================
# (e) Auth header per-request — key rotation transparent
# ===========================================================================


class TestRerankerAuthPerRequest:
    def test_voyage_sends_bearer_auth_on_every_post(self) -> None:
        rec = _RecordingClient(_GOOD_VOYAGE)
        client = _make_voyage(_StubFactory(rec))
        with _patch_cs():
            client.rerank("q", ["doc-a"])
            client.rerank("q", ["doc-b"])
        for call in rec.post_calls:
            headers = call.get("headers") or {}
            assert headers.get("Authorization") == "Bearer k-1084", (
                "Voyage reranker must send the auth header on the per-request .post()"
            )

    def test_cohere_sends_bearer_auth_on_every_post(self) -> None:
        rec = _RecordingClient(_GOOD_COHERE)
        client = _make_cohere(_StubFactory(rec))
        with _patch_cs():
            client.rerank("q", ["doc-a"])
            client.rerank("q", ["doc-b"])
        for call in rec.post_calls:
            headers = call.get("headers") or {}
            assert headers.get("Authorization") == "Bearer k-1084", (
                "Cohere reranker must send the auth header on the per-request .post()"
            )

    def test_voyage_key_rotation_transparent(self) -> None:
        """A new key on the next call must reach the per-request header (no client rebuild)."""
        rec = _RecordingClient(_GOOD_VOYAGE)
        client = _make_voyage(_StubFactory(rec))
        cs1 = _make_config_service("key-old")
        cs2 = _make_config_service("key-new")
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs1,
        ):
            client.rerank("q", ["doc-a"])
        with patch(
            "code_indexer.server.clients.reranker_clients.get_config_service",
            return_value=cs2,
        ):
            client.rerank("q", ["doc-b"])
        assert (rec.post_calls[0].get("headers") or {}).get(
            "Authorization"
        ) == "Bearer key-old"
        assert (rec.post_calls[1].get("headers") or {}).get(
            "Authorization"
        ) == "Bearer key-new"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
