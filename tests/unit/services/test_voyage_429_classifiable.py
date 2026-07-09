"""Voyage query-path 429 must propagate classifiable (Story #1079 Phase A).

The QUERY path is ``get_embeddings_batch(..., retry=False)`` (called by
``get_embedding``). Historically this method wrapped *every* error — including a
429 ``httpx.HTTPStatusError`` surfaced by ``_make_sync_request`` — into a generic
``RuntimeError(str(exc))``. That masked the rate-limit signal so the
``execute_with_backoff`` wrapper never retried it (bug #1078 latent gap).

After Phase A, a 429 from the HTTP boundary on the retry=False path must
propagate as something ``is_rate_limited`` classifies as True, with the
Retry-After header still reachable. Non-429 errors may stay wrapped.

These tests drive the REAL exception path through a fake HTTP transport — they
do NOT mock ``_make_sync_request`` or the wrapping logic itself.
"""

import os
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.code_indexer.config import VoyageAIConfig
from src.code_indexer.services.provider_backoff import (
    get_http_status_error,
    is_rate_limited,
)
from src.code_indexer.services.voyage_ai import VoyageAIClient

_FAKE_KEY = "test-voyage-key-1079A"


def _make_client() -> VoyageAIClient:
    with patch.dict(os.environ, {"VOYAGE_API_KEY": _FAKE_KEY}):
        return VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))


def _install_fake_transport(client: VoyageAIClient, post_fn: Any) -> None:
    fake_client = MagicMock()
    fake_client.__enter__ = lambda s: s
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = post_fn
    fake_factory = MagicMock()
    fake_factory.create_sync_client = MagicMock(return_value=fake_client)
    client._http_client_factory = fake_factory


def _post_status(status_code: int, retry_after: Optional[str] = None) -> Any:
    def fake_post(*args: Any, **kwargs: Any) -> None:
        req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
        headers = {}
        if retry_after is not None:
            headers["retry-after"] = retry_after
        resp = httpx.Response(status_code, headers=headers, request=req)
        raise httpx.HTTPStatusError(f"{status_code}", request=req, response=resp)

    return fake_post


class TestVoyageQueryPath429Classifiable:
    def test_batch_retry_false_429_propagates_classifiable(self):
        """A 429 on get_embeddings_batch(retry=False) must be is_rate_limited True."""
        client = _make_client()
        _install_fake_transport(client, _post_status(429))

        with patch("time.sleep"):  # guard: no in-slot sleep on query path
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        assert is_rate_limited(exc_info.value) is True, (
            f"429 on query path must be classifiable as rate-limited, "
            f"got {type(exc_info.value).__name__}: {exc_info.value}"
        )

    def test_batch_retry_false_429_preserves_retry_after_header(self):
        """The propagated 429 must keep its Retry-After header reachable."""
        client = _make_client()
        _install_fake_transport(client, _post_status(429, retry_after="7.0"))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        http_err = get_http_status_error(exc_info.value)
        assert http_err is not None, "Underlying httpx 429 must be reachable"
        assert http_err.response.headers.get("retry-after") == "7.0"

    def test_get_embedding_query_path_429_propagates_classifiable(self):
        """get_embedding (single, retry=False) also propagates a classifiable 429."""
        client = _make_client()
        _install_fake_transport(client, _post_status(429))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embedding("hello")

        assert is_rate_limited(exc_info.value) is True

    def test_batch_retry_false_non_429_not_classifiable(self):
        """A non-429 (500) on the query path is NOT a rate-limit signal."""
        client = _make_client()
        _install_fake_transport(client, _post_status(500))

        with patch("time.sleep"):
            with pytest.raises(Exception) as exc_info:
                client.get_embeddings_batch(["hello"], retry=False)

        assert is_rate_limited(exc_info.value) is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
