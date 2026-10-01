"""Story #1586 AC2: cidx.embedding.* metrics wired into CohereEmbeddingProvider.

Proves the WIRING -- a real call into CohereEmbeddingProvider._make_sync_request
emits real cidx.embedding.* OTEL metrics via ApplicationMetrics. Uses a real
httpx.Client wired to httpx.MockTransport (the same pattern as
tests/unit/services/test_cohere_embedding_stats_1418.py) -- real client,
real request/response cycle, fake network layer only.
"""

from __future__ import annotations

import os
from typing import Callable
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import CohereConfig
from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)

EMBEDDING_DIM = 1536


class _FakeSyncClientFactory:
    """Real httpx.Client wired to a MockTransport; closed by the code under
    test's own `with _client_ctx as client:` block."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_sync_client(self, *, transport=None, pooled: bool = False, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler))


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"CO_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


class TestCohereMakeSyncRequestEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"embeddings": {"float": [[0.1] * EMBEDDING_DIM]}},
            )

        config = CohereConfig(model="embed-v4.0", max_retries=3, retry_delay=0.01)
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            provider._make_sync_request(["hello world"], retry=False)

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None, "cidx.embedding.requests not emitted"
            dp = list(requests_metric.data.data_points)[0]
            assert dp.value == 1
            assert dp.attributes["model"] == "embed-v4.0"
            assert dp.attributes["status"] == "success"

            tokens_metric = find_metric(reader, "cidx.embedding.tokens")
            assert tokens_metric is not None
            tokens_dp = list(tokens_metric.data.data_points)[0]
            assert tokens_dp.value > 0
            assert tokens_dp.attributes["model"] == "embed-v4.0"
            assert tokens_dp.attributes["status"] == "success"

            duration_metric = find_metric(reader, "cidx.embedding.duration")
            assert duration_metric is not None
            duration_dps = list(duration_metric.data.data_points)
            assert len(duration_dps) == 1
            assert duration_dps[0].attributes["model"] == "embed-v4.0"
            assert duration_dps[0].attributes["status"] == "success"

    def test_failure_records_embedding_metric_status_error(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        config = CohereConfig(
            model="embed-v4.0",
            max_retries=0,
            retry_delay=0.01,
        )
        provider = CohereEmbeddingProvider(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                provider._make_sync_request(["hello"], retry=False)

            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.attributes["model"] == "embed-v4.0"
            assert dp.attributes["status"] == "error"

            tokens_metric = find_metric(reader, "cidx.embedding.tokens")
            assert tokens_metric is not None, (
                "cidx.embedding.tokens must still be recorded (0) on the error path"
            )
            tokens_dp = list(tokens_metric.data.data_points)[0]
            assert tokens_dp.value == 0
            assert tokens_dp.attributes["model"] == "embed-v4.0"
            assert tokens_dp.attributes["status"] == "error"

            duration_metric = find_metric(reader, "cidx.embedding.duration")
            assert duration_metric is not None, (
                "cidx.embedding.duration must still be recorded on the error path"
            )
            duration_dps = list(duration_metric.data.data_points)
            assert len(duration_dps) == 1
            assert duration_dps[0].attributes["model"] == "embed-v4.0"
            assert duration_dps[0].attributes["status"] == "error"
