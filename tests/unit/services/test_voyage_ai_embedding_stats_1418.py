"""Story #1418 Phase 2 of 3: embedding-stats instrumentation for VoyageAIClient.

Injection point 1: VoyageAIClient._make_sync_request -- per-attempt wrap
(the method has an internal retry loop; each retry is a separate real,
billable vendor call, so the wrap lives inside `_single_attempt()`, not the
outer method boundary).

Injection point 2: VoyageAIClient._make_sync_contextualized_request -- same
per-attempt wrap, targeting the contextualized-embeddings endpoint used
exclusively by the per-commit temporal contextual embedder (voyage-context-4)
-- purpose is always "temporal" for this endpoint.

Uses a real httpx.Client wired to httpx.MockTransport (no mocking of the
code under test) so the actual _single_attempt() closure (post(), then
raise_for_status()) executes for real against a controlled fake transport.
"""

from __future__ import annotations

import os
from typing import Callable, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_ai import VoyageAIClient


class _FakeSyncClientFactory:
    """Minimal SyncClientFactory returning an httpx.Client wired to a
    MockTransport -- a real client, real request/response cycle, fake
    network layer only."""

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
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


def _install_writer() -> _RecordingWriter:
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    writer = _RecordingWriter()
    EmbeddingStatsWriter.set_active(writer)
    return writer


class TestMakeSyncRequestQueryPathRecordsSuccess:
    def test_single_attempt_success_records_one_row_purpose_query(self, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

        config = VoyageAIConfig(model="voyage-code-3", max_retries=3, retry_delay=0.01)
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        client._make_sync_request(["hello"], retry=False)

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.provider == "voyageai"
        assert rec.call_type == "embed"
        assert rec.model == "voyage-code-3"
        assert rec.purpose == "query"
        assert rec.success is True
        assert rec.item_count == 1


class TestMakeSyncRequestIndexingPathRetryCounting:
    def test_transient_failure_then_success_produces_two_rows_one_per_attempt(
        self, mock_api_key
    ):
        """A transient failure then success on the INDEXING (retry=True)
        path must produce N rows, one per real HTTP attempt -- not one for
        the outer call."""
        writer = _install_writer()
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})

        config = VoyageAIConfig(
            model="voyage-code-3",
            max_retries=3,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        client._make_sync_request(["hello"], retry=True)

        assert call_count["n"] == 2
        assert len(writer.records) == 2
        assert writer.records[0].success is False
        assert writer.records[0].purpose == "index"
        assert writer.records[1].success is True
        assert writer.records[1].purpose == "index"

    def test_vendor_4xx_5xx_recorded_as_success_false_not_true(self, mock_api_key):
        """Every attempt in an all-failing retry loop is recorded as
        success=False -- a vendor error is never misrecorded as success."""
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        config = VoyageAIConfig(
            model="voyage-code-3",
            max_retries=1,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with pytest.raises(Exception):
            client._make_sync_request(["hello"], retry=True)

        assert len(writer.records) == 2  # initial attempt + 1 retry
        assert all(r.success is False for r in writer.records)


class TestMakeSyncContextualizedRequestAlwaysTemporalPurpose:
    def test_contextualized_endpoint_records_purpose_temporal(self, mock_api_key):
        writer = _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "index": 0,
                            "data": [{"index": 0, "embedding": [0.1] * 1024}],
                        }
                    ]
                },
            )

        config = VoyageAIConfig(
            model="voyage-context-4", max_retries=3, retry_delay=0.01
        )
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        client._make_sync_contextualized_request(
            [["chunk1"]], input_type="document", output_dimension=1024, retry=False
        )

        assert len(writer.records) == 1
        rec = writer.records[0]
        assert rec.call_type == "embed"
        assert rec.purpose == "temporal"
        assert rec.success is True


class TestNoRegressionInReturnedResult:
    def test_success_response_data_unchanged_by_instrumentation(self, mock_api_key):
        _install_writer()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"embedding": [0.2] * 1024}]})

        config = VoyageAIConfig(model="voyage-code-3")
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        result = client._make_sync_request(["hello"], retry=False)
        assert result["data"][0]["embedding"] == [0.2] * 1024
