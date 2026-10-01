"""Story #1586 AC2: cidx.embedding.* metrics wired into VoyageMultimodalClient.

Proves the WIRING -- a real call into VoyageMultimodalClient.get_multimodal_embedding
/ _submit_multimodal_batch emits real cidx.embedding.* OTEL metrics via
ApplicationMetrics.

Neither method has an internal retry loop (single httpx-client post() +
raise_for_status() call each), so wrapping the method boundary is
equivalent to per-attempt. Uses the http_client_factory injection seam
(mirrors the pattern already used in
tests/unit/services/test_voyage_ai_embedding_stats_1418.py for
VoyageAIClient) -- a real httpx.Client wired to httpx.MockTransport, no
monkeypatching of httpx.Client itself.
"""

from __future__ import annotations

import os
from typing import Callable
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_multimodal import VoyageMultimodalClient

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)

EMBEDDING_DIM = 1024
MODEL_NAME = "voyage-multimodal-3"


class _FakeSyncClientFactory:
    """Real httpx.Client wired to a MockTransport; closed by the code under
    test's own `with _client_ctx as client:` block."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_sync_client(self, *, transport=None, pooled: bool = False, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler))


def _success_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        httpx.codes.OK, json={"data": [{"embedding": [0.1] * EMBEDDING_DIM}]}
    )


def _error_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        httpx.codes.INTERNAL_SERVER_ERROR, json={"error": "server error"}
    )


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


@pytest.fixture
def make_client(mock_api_key):
    """Factory fixture: build a VoyageMultimodalClient wired to the given
    fake HTTP handler via the real http_client_factory injection seam."""

    def _make(
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> VoyageMultimodalClient:
        return VoyageMultimodalClient(
            VoyageAIConfig(model=MODEL_NAME),
            http_client_factory=_FakeSyncClientFactory(handler),
        )

    return _make


def _assert_success_metrics(reader, model: str) -> None:
    requests_metric = find_metric(reader, "cidx.embedding.requests")
    assert requests_metric is not None, "cidx.embedding.requests not emitted"
    dp = list(requests_metric.data.data_points)[0]
    assert dp.value == 1
    assert dp.attributes["model"] == model
    assert dp.attributes["status"] == "success"

    tokens_metric = find_metric(reader, "cidx.embedding.tokens")
    assert tokens_metric is not None
    tokens_dp = list(tokens_metric.data.data_points)[0]
    assert tokens_dp.value > 0
    assert tokens_dp.attributes["model"] == model
    assert tokens_dp.attributes["status"] == "success"

    duration_metric = find_metric(reader, "cidx.embedding.duration")
    assert duration_metric is not None
    duration_dps = list(duration_metric.data.data_points)
    assert len(duration_dps) == 1
    assert duration_dps[0].attributes["model"] == model
    assert duration_dps[0].attributes["status"] == "success"


def _assert_error_metrics(reader, model: str) -> None:
    requests_metric = find_metric(reader, "cidx.embedding.requests")
    assert requests_metric is not None
    dp = list(requests_metric.data.data_points)[0]
    assert dp.value == 1
    assert dp.attributes["model"] == model
    assert dp.attributes["status"] == "error"

    tokens_metric = find_metric(reader, "cidx.embedding.tokens")
    assert tokens_metric is not None, (
        "cidx.embedding.tokens must still be recorded (0) on the error path"
    )
    tokens_dp = list(tokens_metric.data.data_points)[0]
    assert tokens_dp.value == 0
    assert tokens_dp.attributes["model"] == model
    assert tokens_dp.attributes["status"] == "error"

    duration_metric = find_metric(reader, "cidx.embedding.duration")
    assert duration_metric is not None, (
        "cidx.embedding.duration must still be recorded on the error path"
    )
    duration_dps = list(duration_metric.data.data_points)
    assert len(duration_dps) == 1
    assert duration_dps[0].attributes["model"] == model
    assert duration_dps[0].attributes["status"] == "error"


class TestGetMultimodalEmbeddingEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics(self, make_client):
        client = make_client(_success_handler)

        with active_application_metrics_singleton() as (_metrics, reader):
            client.get_multimodal_embedding("a photo of a cat", [])
            _assert_success_metrics(reader, MODEL_NAME)

    def test_failure_records_embedding_metric_status_error(self, make_client):
        client = make_client(_error_handler)

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                client.get_multimodal_embedding("a photo of a cat", [])
            _assert_error_metrics(reader, MODEL_NAME)


class TestSubmitMultimodalBatchEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics(self, make_client):
        client = make_client(_success_handler)

        with active_application_metrics_singleton() as (_metrics, reader):
            client._submit_multimodal_batch([{"text": "a photo of a cat"}])
            _assert_success_metrics(reader, MODEL_NAME)

    def test_failure_records_embedding_metric_status_error(self, make_client):
        client = make_client(_error_handler)

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                client._submit_multimodal_batch([{"text": "a photo of a cat"}])
            _assert_error_metrics(reader, MODEL_NAME)
