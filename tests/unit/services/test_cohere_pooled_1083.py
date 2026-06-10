"""Story #1083: CohereEmbeddingProvider uses the pooled keep-alive client.

Cohere already sends auth on the per-request .post() call; this test pins the
remaining requirement: the production path borrows the factory's ONE long-lived
keep-alive client via create_sync_client(pooled=True) instead of building +
closing a fresh client (and TLS handshake) per query.

Unit-level; no real provider calls.
"""

from typing import Any, Dict

import httpx

from src.code_indexer.config import CohereConfig
from src.code_indexer.services.cohere_embedding import CohereEmbeddingProvider


_FAKE_KEY = "test-cohere-key-1083"

_GOOD_RESPONSE: Dict[str, Any] = {
    "embeddings": {"float": [[0.1] * 1024]},
}


class _RecordingFactory:
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


def _make_provider() -> CohereEmbeddingProvider:
    config = CohereConfig(api_key=_FAKE_KEY)
    return CohereEmbeddingProvider(config)


def test_cohere_requests_pooled_client() -> None:
    """Cohere production path must opt into the pooled keep-alive client."""
    provider = _make_provider()
    factory = _RecordingFactory()
    provider._http_client_factory = factory  # type: ignore[assignment]

    provider._make_sync_request(["hello"], retry=False)

    assert len(factory.create_calls) == 1
    assert factory.create_calls[0].get("pooled") is True, (
        f"Expected pooled=True, got {factory.create_calls[0]!r}"
    )


def test_cohere_auth_header_per_request() -> None:
    """Authorization Bearer header must travel on .post() (auth-agnostic client)."""
    provider = _make_provider()
    factory = _RecordingFactory()
    provider._http_client_factory = factory  # type: ignore[assignment]

    provider._make_sync_request(["hello"], retry=False)

    assert len(factory.post_calls) == 1
    _url, post_kwargs = factory.post_calls[0]
    headers = post_kwargs.get("headers") or {}
    assert headers.get("Authorization") == f"Bearer {_FAKE_KEY}"


def test_cohere_borrowed_client_not_closed() -> None:
    """The provider must not close the borrowed pooled client."""
    provider = _make_provider()
    factory = _RecordingFactory()
    provider._http_client_factory = factory  # type: ignore[assignment]

    provider._make_sync_request(["hello"], retry=False)

    assert factory.client_closed is False
