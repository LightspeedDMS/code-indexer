"""Story #1586 AC2: cidx.embedding.* metrics wired into CohereMultimodalClient.

Proves the WIRING -- a real call into CohereMultimodalClient._make_request
emits real cidx.embedding.* OTEL metrics via ApplicationMetrics.

_make_request constructs httpx.Client(...) directly (no factory injection),
so -- mirroring tests/unit/services/test_cohere_multimodal_stats_1418.py --
these tests monkeypatch httpx.Client globally to route through
httpx.MockTransport: a real httpx.Client, real request/response cycle, fake
network layer only.
"""

from __future__ import annotations

import os
from typing import Callable
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import CohereConfig
from code_indexer.services.cohere_multimodal import CohereMultimodalClient

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)

EMBEDDING_DIM = 1536
MODEL_NAME = "embed-v4.0"


def _patch_httpx_client_with_mock_transport(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client_cls = httpx.Client

    def _fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client_cls(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", _fake_client)


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"CO_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


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


class TestCohereMultimodalMakeRequestEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics(self, mock_api_key, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"embeddings": {"float": [[0.1] * EMBEDDING_DIM]}}
            )

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = CohereConfig(model=MODEL_NAME, max_retries=3, retry_delay=0.01)
        client = CohereMultimodalClient(config)

        with active_application_metrics_singleton() as (_metrics, reader):
            client._make_request(
                [{"content": [{"type": "text", "text": "a photo of a cat"}]}],
                "search_document",
            )
            _assert_success_metrics(reader, MODEL_NAME)

    def test_failure_records_embedding_metric_status_error(
        self, mock_api_key, monkeypatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        _patch_httpx_client_with_mock_transport(monkeypatch, handler)

        config = CohereConfig(model=MODEL_NAME, max_retries=0, retry_delay=0.01)
        client = CohereMultimodalClient(config)

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                client._make_request(
                    [{"content": [{"type": "text", "text": "a photo of a cat"}]}],
                    "search_document",
                )
            _assert_error_metrics(reader, MODEL_NAME)
