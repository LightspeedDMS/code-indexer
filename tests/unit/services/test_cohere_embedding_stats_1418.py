"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for
CohereEmbeddingProvider._make_sync_request (injection point 3).

Per-attempt wrap (internal retry loop) -- each retry is a separate real,
billable vendor call.
"""

from __future__ import annotations

import os
from typing import Callable, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import CohereConfig
from code_indexer.services.cohere_embedding import CohereEmbeddingProvider


class _FakeSyncClientFactory:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_sync_client(self, *, transport=None, pooled: bool = False, **kwargs):
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


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"CO_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


def _install_writer() -> _RecordingWriter:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    writer = _RecordingWriter()
    EmbeddingStatsWriter.set_active(writer)
    return writer


class TestCohereMakeSyncRequestQueryPath:
    def test_single_attempt_success_records_one_row_purpose_query(self, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 1536]}})

        config = CohereConfig(model="embed-v4.0", max_retries=3, retry_delay=0.01)
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        provider._make_sync_request(["hello"], retry=False)

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "cohere"
        assert rec.call_type == "embed"
        assert rec.model == "embed-v4.0"
        assert rec.purpose == "query"
        assert rec.success is True

    def test_client_4xx_error_recorded_as_success_false(self, mock_api_key):
        """A vendor 400 (client error, no retry) must record success=False,
        never success=True."""
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad request"})

        config = CohereConfig(model="embed-v4.0", max_retries=3, retry_delay=0.01)
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with pytest.raises(Exception):
            provider._make_sync_request(["hello"], retry=False)

        assert len(writer.records) == 1
        assert writer.records[0].success is False


class TestCohereMakeSyncRequestIndexingRetryCounting:
    def test_transient_failure_then_success_produces_two_rows(self, mock_api_key):
        writer = _install_writer()
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json={"embeddings": {"float": [[0.1] * 1536]}})

        config = CohereConfig(
            model="embed-v4.0",
            max_retries=3,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        provider._make_sync_request(["hello"], retry=True)

        assert call_count["n"] == 2
        assert len(writer.records) == 2
        assert writer.records[0].success is False
        assert writer.records[0].purpose == "index"
        assert writer.records[1].success is True

    def test_vendor_5xx_error_recorded_as_success_false(self, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        config = CohereConfig(
            model="embed-v4.0",
            max_retries=1,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with pytest.raises(Exception):
            provider._make_sync_request(["hello"], retry=True)

        assert len(writer.records) == 2
        assert all(r.success is False for r in writer.records)
