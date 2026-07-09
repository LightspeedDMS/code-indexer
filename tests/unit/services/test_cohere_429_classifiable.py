"""Cohere query-path 429 must propagate classifiable (Story #1079 Phase A).

Symmetric guard to test_voyage_429_classifiable.py. The QUERY path is
``get_embeddings_batch(..., retry=False)`` (called by ``get_embedding``). A 429
from the HTTP boundary must propagate as something ``is_rate_limited`` classifies
as True (with Retry-After reachable), so the ``execute_with_backoff`` wrapper —
and the future AIMD signal — can see it. Non-429 errors need not be classifiable.

These tests drive the REAL exception path through a fake HTTP transport; they do
NOT mock ``_make_sync_request`` or the exception-wrapping logic.
"""

import os
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.code_indexer.config import CohereConfig
from src.code_indexer.services.cohere_embedding import CohereEmbeddingProvider
from src.code_indexer.services.provider_backoff import (
    get_http_status_error,
    is_rate_limited,
)

_FAKE_KEY = "test-cohere-key-1079A"

_GOOD_RESPONSE: Dict[str, Any] = {
    "embeddings": {"float": [[0.1] * 1536]},
    "meta": {"billed_units": {"input_tokens": 5}},
}


def _make_client() -> CohereEmbeddingProvider:
    with patch.dict(os.environ, {"CO_API_KEY": _FAKE_KEY}):
        return CohereEmbeddingProvider(CohereConfig())


def _install_fake_transport(client: CohereEmbeddingProvider, post_fn: Any) -> None:
    fake_client = MagicMock()
    fake_client.__enter__ = lambda s: s
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = post_fn
    fake_factory = MagicMock()
    fake_factory.create_sync_client = MagicMock(return_value=fake_client)
    client._http_client_factory = fake_factory


def _post_status(status_code: int, retry_after: Optional[str] = None) -> Any:
    def fake_post(*args: Any, **kwargs: Any) -> None:
        req = httpx.Request("POST", "https://api.cohere.com/v2/embed")
        headers = {}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        resp = httpx.Response(status_code, headers=headers, request=req)
        raise httpx.HTTPStatusError(f"{status_code}", request=req, response=resp)

    return fake_post


class TestCohereQueryPath429Classifiable:
    def test_batch_retry_false_429_propagates_classifiable(self):
        client = _make_client()
        _install_fake_transport(client, _post_status(429))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        assert is_rate_limited(exc_info.value) is True, (
            f"Cohere 429 on query path must be classifiable as rate-limited, "
            f"got {type(exc_info.value).__name__}: {exc_info.value}"
        )

    def test_batch_retry_false_429_preserves_retry_after_header(self):
        client = _make_client()
        _install_fake_transport(client, _post_status(429, retry_after="4.0"))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        http_err = get_http_status_error(exc_info.value)
        assert http_err is not None, "Underlying httpx 429 must be reachable"
        assert http_err.response.headers.get("retry-after") == "4.0"

    def test_get_embedding_query_path_429_propagates_classifiable(self):
        client = _make_client()
        _install_fake_transport(client, _post_status(429))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embedding("hello")

        assert is_rate_limited(exc_info.value) is True

    def test_batch_retry_false_non_429_not_classifiable(self):
        client = _make_client()
        _install_fake_transport(client, _post_status(500))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        assert is_rate_limited(exc_info.value) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
