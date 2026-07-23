"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for reranker
clients (injection points 5 and 6).

Method-boundary wrap sufficient (no internal retry loop -- each retry from
the caller is already a fresh top-level call). The instrumented unit wraps
the caller's code block that invokes `_post()` AND its subsequent
`raise_for_status()` check together as ONE atomic unit -- `_post()` alone
performs the network call only; `rerank()` (the caller) invokes
raise_for_status() separately, so wrapping `_post()` alone would misclassify
a vendor 4xx/5xx as success=True.

API-key preflight (inside `_post()`, raised as ValueError BEFORE any network
call) must produce NO row -- a call that never reaches the network is not a
real vendor call.
"""

from __future__ import annotations

import os
from typing import Callable, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.server.clients.reranker_clients import (
    CohereRerankerClient,
    VoyageRerankerClient,
)


class _FakeSyncClientFactory:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_sync_client(self, *, timeout=None, pooled: bool = False, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler))


class _RecordingWriter:
    def __init__(self):
        self.records: List = []

    def record(self, call) -> None:
        self.records.append(call)

    def flush(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_active_writer():
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    EmbeddingStatsWriter._active = None
    yield
    EmbeddingStatsWriter._active = None


def _install_writer() -> _RecordingWriter:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    writer = _RecordingWriter()
    EmbeddingStatsWriter.set_active(writer)
    return writer


class TestVoyageRerankerClientSuccess:
    def test_rerank_success_records_one_row_purpose_query(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "relevance_score": 0.9}]},
            )

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            client = VoyageRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            client.rerank("query", ["doc1"])

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "voyageai"
        assert rec.call_type == "rerank"
        assert rec.purpose == "query"
        assert rec.success is True
        assert rec.item_count == 1


class TestVoyageRerankerClientVendorError:
    def test_vendor_4xx_via_raise_for_status_recorded_as_success_false(self):
        """rerank() calls _post() then raise_for_status() SEPARATELY -- the
        instrumented unit must wrap both together, or a vendor 4xx would be
        misrecorded as success=True."""
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
            client = VoyageRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.rerank("query", ["doc1"])

        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestVoyageRerankerClientApiKeyPreflight:
    def test_missing_api_key_produces_no_row(self):
        """A call that never reaches the network (preflight ValueError from
        _post()) must never be recorded as a real vendor call attempt."""
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be reached")

        with patch.dict(os.environ, {}, clear=True):
            client = VoyageRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            with pytest.raises(ValueError, match="API key not found"):
                client.rerank("query", ["doc1"])

        assert len(writer.records) == 0


class TestCohereRerankerClientSuccess:
    def test_rerank_success_records_one_row_purpose_query(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"results": [{"index": 0, "relevance_score": 0.9}]},
            )

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            client = CohereRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            client.rerank("query", ["doc1"])

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "cohere"
        assert rec.call_type == "rerank"
        assert rec.purpose == "query"
        assert rec.success is True


class TestCohereRerankerClientVendorError:
    def test_vendor_4xx_via_raise_for_status_recorded_as_success_false(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
            client = CohereRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.rerank("query", ["doc1"])

        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestCohereRerankerClientApiKeyPreflight:
    def test_missing_api_key_produces_no_row(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("network must not be reached")

        with patch.dict(os.environ, {}, clear=True):
            client = CohereRerankerClient(
                http_client_factory=_FakeSyncClientFactory(handler)
            )
            with pytest.raises(ValueError, match="API key not found"):
                client.rerank("query", ["doc1"])

        assert len(writer.records) == 0
