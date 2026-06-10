"""Story #1083: VoyageAIClient uses a pooled keep-alive client + per-request auth.

The production embedding hot path must:
  - request the client via create_sync_client(pooled=True) so the factory's
    long-lived keep-alive client is reused (no per-query TLS handshake), and
  - send the Authorization: Bearer <key> header on each .post() call rather than
    baking it into the (shared, auth-agnostic) client — so API-key rotation is
    transparent and needs no client rebuild.

All tests are unit-level and do NOT call real providers.
"""

import os
from typing import Any, Dict
from unittest.mock import patch

import httpx

from src.code_indexer.config import VoyageAIConfig
from src.code_indexer.services.voyage_ai import VoyageAIClient


_FAKE_KEY = "test-voyage-key-1083"
_ROTATED_KEY = "rotated-voyage-key-1083"

_GOOD_RESPONSE: Dict[str, Any] = {
    "data": [{"embedding": [0.1] * 1024}],
    "usage": {"total_tokens": 5},
}


def _make_client(api_key: str = _FAKE_KEY) -> VoyageAIClient:
    with patch.dict(os.environ, {"VOYAGE_API_KEY": api_key}):
        return VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))


class _RecordingFactory:
    """Records create_sync_client kwargs and the .post() calls on the client."""

    def __init__(self) -> None:
        self.create_calls: list = []
        self.post_calls: list = []
        self.client_closed = False

        factory = self

        class _Client:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def close(self_inner):
                factory.client_closed = True

            def post(self_inner, url, **kwargs):
                factory.post_calls.append((url, kwargs))
                req = httpx.Request("POST", url)
                return httpx.Response(200, json=_GOOD_RESPONSE, request=req)

        self._client = _Client()

    def create_sync_client(self, **kwargs: Any):
        self.create_calls.append(kwargs)
        return self._client


def test_make_sync_request_requests_pooled_client() -> None:
    """The provider must opt into the pooled keep-alive client (pooled=True)."""
    client = _make_client()
    factory = _RecordingFactory()
    client._http_client_factory = factory  # type: ignore[assignment]

    client._make_sync_request(["hello"], retry=False)

    assert len(factory.create_calls) == 1
    assert factory.create_calls[0].get("pooled") is True, (
        f"Expected pooled=True in create_sync_client call, got {factory.create_calls[0]!r}"
    )


def test_auth_header_is_per_request_not_on_client() -> None:
    """Authorization must travel on .post(), not be baked into the client."""
    client = _make_client()
    factory = _RecordingFactory()
    client._http_client_factory = factory  # type: ignore[assignment]

    client._make_sync_request(["hello"], retry=False)

    # Client must NOT be built with baked-in auth headers.
    create_kwargs = factory.create_calls[0]
    baked_headers = create_kwargs.get("headers") or {}
    assert "Authorization" not in baked_headers, (
        "Pooled client must be auth-agnostic — no Authorization baked into the client"
    )

    # The .post() call must carry the Authorization Bearer header.
    assert len(factory.post_calls) == 1
    _url, post_kwargs = factory.post_calls[0]
    post_headers = post_kwargs.get("headers") or {}
    assert post_headers.get("Authorization") == f"Bearer {_FAKE_KEY}", (
        f"Expected per-request Authorization Bearer header, got {post_headers!r}"
    )


def test_key_rotation_reflected_without_client_rebuild() -> None:
    """A rotated api_key is reflected on the NEXT request's header."""
    client = _make_client()
    factory = _RecordingFactory()
    client._http_client_factory = factory  # type: ignore[assignment]

    client._make_sync_request(["hello"], retry=False)
    # Rotate the key on the existing client instance (no new client constructed).
    client.api_key = _ROTATED_KEY
    client._make_sync_request(["world"], retry=False)

    assert len(factory.post_calls) == 2
    assert factory.post_calls[0][1]["headers"]["Authorization"] == f"Bearer {_FAKE_KEY}"
    assert (
        factory.post_calls[1][1]["headers"]["Authorization"] == f"Bearer {_ROTATED_KEY}"
    )


def test_borrowed_client_not_closed_per_call() -> None:
    """The provider must not close the pooled client after a request (borrow)."""
    client = _make_client()
    factory = _RecordingFactory()
    client._http_client_factory = factory  # type: ignore[assignment]

    client._make_sync_request(["hello"], retry=False)

    assert factory.client_closed is False, (
        "Provider must borrow (not close) the pooled client"
    )
