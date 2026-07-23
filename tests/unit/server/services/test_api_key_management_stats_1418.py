"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for
ApiKeyConnectivityTester (injection points 9 and 10 -- VoyageAI and Cohere
key-connectivity-test calls).

purpose="key_test" (VALID_PURPOSES already supports it per Phase 1's
EmbeddingCallRecord). These are async methods, so the async
instrument_call_async() variant is used.
"""

from __future__ import annotations

from typing import Callable, List

import httpx
import pytest

from code_indexer.server.services.api_key_management import ApiKeyConnectivityTester


class _FakeAsyncHttpClientFactory:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_client(self, *, timeout=None, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))


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


class TestVoyageAIConnectivitySuccess:
    @pytest.mark.asyncio
    async def test_success_records_one_row_purpose_key_test(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})

        tester = ApiKeyConnectivityTester(
            http_client_factory=_FakeAsyncHttpClientFactory(handler)
        )

        result = await tester.test_voyageai_connectivity("test-key")

        assert result.success is True
        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "voyageai"
        assert rec.call_type == "embed"
        assert rec.purpose == "key_test"
        assert rec.success is True


class TestVoyageAIConnectivityFailure:
    @pytest.mark.asyncio
    async def test_4xx_recorded_as_success_false(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        tester = ApiKeyConnectivityTester(
            http_client_factory=_FakeAsyncHttpClientFactory(handler)
        )

        result = await tester.test_voyageai_connectivity("bad-key")

        assert result.success is False
        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestCohereConnectivitySuccess:
    @pytest.mark.asyncio
    async def test_success_records_one_row_purpose_key_test(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": {"float": [[0.1]]}})

        tester = ApiKeyConnectivityTester(
            http_client_factory=_FakeAsyncHttpClientFactory(handler)
        )

        result = await tester.test_cohere_connectivity("test-key")

        assert result.success is True
        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "cohere"
        assert rec.call_type == "embed"
        assert rec.purpose == "key_test"
        assert rec.success is True


class TestCohereConnectivityFailure:
    @pytest.mark.asyncio
    async def test_4xx_recorded_as_success_false(self):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        tester = ApiKeyConnectivityTester(
            http_client_factory=_FakeAsyncHttpClientFactory(handler)
        )

        result = await tester.test_cohere_connectivity("bad-key")

        assert result.success is False
        assert len(writer.records) == 1
        assert writer.records[0].success is False
