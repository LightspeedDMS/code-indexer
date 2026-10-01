"""Tests for C2 fix (Bug #1078): get_embedding must not sleep internally.

get_embedding is the QUERY path and is always wrapped by execute_with_backoff
OUTSIDE the governor slot. Therefore it must call _make_sync_request with
retry=False so that:
  - Exactly ONE HTTP attempt is made per governor slot acquisition.
  - No time.sleep() runs while the governor slot is held.
  - 429 propagates immediately to execute_with_backoff (already the case).
  - 500/generic errors propagate immediately (no in-slot back-off sleep).

get_embeddings_batch is the INDEXING path and must retain its retry loop
(retry=True default).

All tests are unit-level and do NOT call real providers.
"""

import os
import sys
import time as _real_time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_ai import VoyageAIClient

# The module object whose `time` name voyage_ai's methods resolve at call time.
_VOYAGE_MODULE = sys.modules[VoyageAIClient.__module__]


@contextmanager
def _voyage_sleep_spy() -> Iterator[List[float]]:
    """Record time.sleep calls originating ONLY from voyage_ai code.

    Determinism fix (flaky under fast-automation parallel load): the old
    approach patched the *process-global* ``time.sleep`` and asserted the
    recorded list was empty. That is unreliable in the full suite because
    unrelated background daemon threads (schedulers, metrics writers, etc.)
    call ``time.sleep`` during the patch window and append to the same list,
    so a "no sleep" assertion spuriously fails — while it always passes in
    isolation (no such threads running).

    Rebinding the ``time`` NAME inside the voyage_ai module isolates the spy
    to the code under test: other threads keep using the real ``time`` module
    and cannot pollute this list. ``time()``/``monotonic()`` still delegate to
    real time so voyage_ai's latency bookkeeping is unaffected.

    Proven with a background sleeper: global patch pollutes ~22/50 iterations;
    this module-scoped spy pollutes 0/50.
    """
    sleep_calls: List[float] = []
    fake_time = SimpleNamespace(
        sleep=lambda t: sleep_calls.append(t),
        time=_real_time.time,
        monotonic=_real_time.monotonic,
    )
    with patch.object(_VOYAGE_MODULE, "time", fake_time):
        yield sleep_calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_KEY = "test-voyage-key-c2"

_GOOD_RESPONSE: Dict[str, Any] = {
    "data": [{"embedding": [0.1] * 1024}],
    "usage": {"total_tokens": 5},
}


def _make_client() -> VoyageAIClient:
    with patch.dict(os.environ, {"VOYAGE_API_KEY": _FAKE_KEY}):
        return VoyageAIClient(VoyageAIConfig(model="voyage-code-3"))


def _install_fake_transport(client: VoyageAIClient, post_fn: Any) -> None:
    """Wire a fake HTTP transport into client._http_client_factory."""
    fake_client = MagicMock()
    fake_client.__enter__ = lambda s: s
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.post = post_fn
    fake_factory = MagicMock()
    fake_factory.create_sync_client = MagicMock(return_value=fake_client)
    client._http_client_factory = fake_factory


# ---------------------------------------------------------------------------
# C2a: get_embedding forwards retry=False to _make_sync_request
# ---------------------------------------------------------------------------


class TestGetEmbeddingUsesRetryFalse:
    """get_embedding must call _make_sync_request(texts, model, retry=False)."""

    def test_get_embedding_passes_retry_false(self):
        """_make_sync_request is invoked with retry=False from get_embedding."""
        client = _make_client()
        with patch.object(
            client, "_make_sync_request", return_value=_GOOD_RESPONSE
        ) as mock_req:
            client.get_embedding("hello")
        assert mock_req.call_count == 1
        _args, _kwargs = mock_req.call_args
        assert _kwargs.get("retry") is False, (
            f"Expected retry=False in _make_sync_request call, got kwargs={_kwargs}"
        )

    def test_get_embeddings_batch_uses_retry_true_by_default(self):
        """get_embeddings_batch must call _make_sync_request with retry=True (default)."""
        client = _make_client()
        with patch.object(
            client, "_make_sync_request", return_value=_GOOD_RESPONSE
        ) as mock_req:
            client.get_embeddings_batch(["hello"])
        assert mock_req.call_count == 1
        _args, _kwargs = mock_req.call_args
        retry_val = _kwargs.get("retry", True)
        assert retry_val is True, (
            f"Expected retry=True in indexing path, got kwargs={_kwargs}"
        )


# ---------------------------------------------------------------------------
# C2b: _make_sync_request(retry=False) makes exactly ONE attempt, no sleep
# ---------------------------------------------------------------------------


class TestMakeSyncRequestRetryFalse:
    """_make_sync_request(retry=False) must make one attempt and raise immediately."""

    def test_single_attempt_on_500_no_sleep(self):
        """On 500 with retry=False: exactly one HTTP call, time.sleep never called."""
        client = _make_client()
        call_count = 0

        def fake_post(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError("server error", request=req, response=resp)

        _install_fake_transport(client, fake_post)

        with _voyage_sleep_spy() as sleep_calls:
            with pytest.raises(Exception):
                client._make_sync_request(["hello"], retry=False)

        assert call_count == 1, (
            f"retry=False must make exactly 1 HTTP call, got {call_count}"
        )
        assert sleep_calls == [], (
            f"retry=False must NOT call time.sleep, but got calls: {sleep_calls}"
        )

    def test_single_attempt_on_generic_error_no_sleep(self):
        """On network error with retry=False: one call, no sleep."""
        client = _make_client()
        call_count = 0

        def fake_post(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("network down")

        _install_fake_transport(client, fake_post)

        with _voyage_sleep_spy() as sleep_calls:
            with pytest.raises(Exception):
                client._make_sync_request(["hello"], retry=False)

        assert call_count == 1
        assert sleep_calls == []

    def test_single_attempt_success_returns_result(self):
        """On success with retry=False: returns the response normally."""
        client = _make_client()

        def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
            req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
            return httpx.Response(200, json=_GOOD_RESPONSE, request=req)

        _install_fake_transport(client, fake_post)

        result = client._make_sync_request(["hello"], retry=False)
        assert result == _GOOD_RESPONSE


# ---------------------------------------------------------------------------
# C2c: _make_sync_request(retry=True) still retries on 500 (indexing path)
# ---------------------------------------------------------------------------


class TestMakeSyncRequestRetryTrue:
    """retry=True (default) must retain existing retry loop — indexing path unchanged."""

    def test_retries_on_500_when_retry_true(self):
        """With retry=True and 500 error, multiple HTTP calls are made (retry loop active)."""
        client = _make_client()
        call_count = 0

        def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
            if call_count < 3:
                resp = httpx.Response(500, request=req)
                raise httpx.HTTPStatusError("server error", request=req, response=resp)
            return httpx.Response(200, json=_GOOD_RESPONSE, request=req)

        _install_fake_transport(client, fake_post)

        with patch("time.sleep"):
            result = client._make_sync_request(["hello"], retry=True)

        assert call_count >= 2, (
            f"retry=True should retry on 500 errors, but only made {call_count} call(s)"
        )
        assert result == _GOOD_RESPONSE


# ---------------------------------------------------------------------------
# B1: _make_sync_request(retry=True) must RETRY on 429 (indexing path)
# ---------------------------------------------------------------------------


class TestMakeSyncRequestRetryTrue429Retries:
    """B1 (Bug #1078 re-review): retry=True (INDEXING) must retry 429 with backoff.

    The C2 refactor accidentally raised immediately on 429 even for retry=True.
    Indexing batches now silently drop rate-limited chunks because
    VectorCalculationManager converts the exception to a failed VectorResult
    without retrying.  This class validates the restored behaviour:
      - retry=True + 429: multiple HTTP calls made, time.sleep called (backoff active).
      - retry=False + 429: exactly 1 HTTP call, time.sleep NOT called.
    """

    def test_retries_on_429_when_retry_true_and_succeeds(self):
        """retry=True with 429-then-200: must retry and ultimately succeed."""
        client = _make_client()
        call_count = 0

        def fake_post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
            if call_count == 1:
                resp = httpx.Response(429, request=req)
                raise httpx.HTTPStatusError("rate limited", request=req, response=resp)
            return httpx.Response(200, json=_GOOD_RESPONSE, request=req)

        _install_fake_transport(client, fake_post)

        sleep_calls: List[float] = []
        with patch("time.sleep", side_effect=lambda t: sleep_calls.append(t)):
            result = client._make_sync_request(["hello"], retry=True)

        assert call_count >= 2, (
            f"retry=True must retry on 429 (indexing backoff), but only made {call_count} call(s)"
        )
        assert len(sleep_calls) >= 1, (
            f"retry=True must call time.sleep for 429 backoff, but sleep was not called; "
            f"calls={sleep_calls}"
        )
        assert result == _GOOD_RESPONSE

    def test_no_retry_on_429_when_retry_false(self):
        """retry=False with 429: exactly 1 HTTP call, time.sleep NOT called (query path)."""
        client = _make_client()
        call_count = 0

        def fake_post(*args: Any, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            req = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("rate limited", request=req, response=resp)

        _install_fake_transport(client, fake_post)

        with _voyage_sleep_spy() as sleep_calls:
            with pytest.raises(Exception):
                client._make_sync_request(["hello"], retry=False)

        assert call_count == 1, (
            f"retry=False must make exactly 1 HTTP call on 429, got {call_count}"
        )
        assert sleep_calls == [], (
            f"retry=False must NOT call time.sleep on 429, but got: {sleep_calls}"
        )
