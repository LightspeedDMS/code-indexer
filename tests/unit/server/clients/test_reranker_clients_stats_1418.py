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
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    EmbeddingStatsWriter._active = None
    ProviderHealthMonitor.reset_instance()
    yield
    EmbeddingStatsWriter._active = None
    ProviderHealthMonitor.reset_instance()


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


def _voyage_rerank(handler) -> None:
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "test-key"}):
        VoyageRerankerClient(
            http_client_factory=_FakeSyncClientFactory(handler)
        ).rerank("query", ["doc1", "doc2"])


class TestVoyageRerankerRecordsVendorTokens:
    """Issue #2173: token_count is Voyage's usage.total_tokens."""

    def test_success_records_vendor_usage_total_tokens(self):
        writer = _install_writer()

        _voyage_rerank(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "relevance_score": 0.9}],
                    "usage": {"total_tokens": 2},
                },
            )
        )

        assert [r.token_count for r in writer.records] == [2]

    def test_vendor_error_records_zero_tokens(self):
        writer = _install_writer()

        with pytest.raises(httpx.HTTPStatusError):
            _voyage_rerank(
                lambda request: httpx.Response(400, json={"usage": {"total_tokens": 2}})
            )

        assert [(r.success, r.token_count) for r in writer.records] == [(False, 0)]


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


def _cohere_rerank(handler) -> None:
    with patch.dict(os.environ, {"CO_API_KEY": "test-key"}):
        CohereRerankerClient(
            http_client_factory=_FakeSyncClientFactory(handler)
        ).rerank("query", ["doc1", "doc2"])


def _cohere_rerank_response(billed_units: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [{"index": 0, "relevance_score": 0.9}],
            "meta": {"api_version": {"version": "2"}, "billed_units": billed_units},
        },
    )


class TestCohereRerankerRecordsVendorTokens:
    """Issue #2173: Cohere bills rerank in search units, which are not
    tokens -- record 0 (no missing-usage warning) unless input tokens are
    reported."""

    def test_search_units_only_records_zero_without_warning(self, caplog):
        writer = _install_writer()

        with caplog.at_level(
            "WARNING",
            logger="code_indexer.server.services.embedding_call_instrumentation",
        ):
            _cohere_rerank(lambda request: _cohere_rerank_response({"search_units": 1}))

        assert [(r.success, r.token_count) for r in writer.records] == [(True, 0)]
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

    def test_reported_input_tokens_are_recorded(self):
        writer = _install_writer()

        _cohere_rerank(
            lambda request: _cohere_rerank_response(
                {"search_units": 1, "input_tokens": 3}
            )
        )

        assert [r.token_count for r in writer.records] == [3]

    def test_vendor_error_records_zero_tokens(self):
        writer = _install_writer()

        with pytest.raises(httpx.HTTPStatusError):
            _cohere_rerank(
                lambda request: httpx.Response(
                    400, json={"meta": {"billed_units": {"input_tokens": 3}}}
                )
            )

        assert [(r.success, r.token_count) for r in writer.records] == [(False, 0)]


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
