"""Story #1586 AC2: cidx.embedding.* metrics wired into VoyageAIClient.

Proves the WIRING -- a real call into VoyageAIClient._make_sync_request /
_make_sync_contextualized_request emits real cidx.embedding.* OTEL metrics
via ApplicationMetrics -- not just that ApplicationMetrics.record_embedding_request
works standalone, and not just that record_embedding_provider_call() works
standalone (both already covered elsewhere).

Uses a real httpx.Client wired to httpx.MockTransport (the same pattern as
tests/unit/services/test_voyage_ai_embedding_stats_1418.py) -- a real
client, real request/response cycle, fake network layer only. The client is
always consumed by the code under test via `with _client_ctx as client:`
(voyage_ai.py's _do_post_and_validate), whose __exit__ closes it -- no
separate cleanup is needed here.
"""

from __future__ import annotations

import os
from typing import Callable, List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_ai import VoyageAIClient

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)

# Real voyage-code-3 embedding dimensionality -- only the response shape
# matters for these tests (metrics wiring), not the actual vector values.
EMBEDDING_DIM = 1024


class _FakeSyncClientFactory:
    """Minimal SyncClientFactory returning an httpx.Client wired to a
    MockTransport -- a real client, real request/response cycle, fake
    network layer only. The returned client is closed by the code under
    test's own `with _client_ctx as client:` block."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self._handler = handler

    def create_sync_client(self, *, transport=None, pooled: bool = False, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler))


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


def _assert_success_metrics(reader, model: str) -> None:
    """Assert cidx.embedding.requests/tokens/duration all recorded once,
    status=success, with the given model attribute and a real (>0) token
    count."""
    requests_metric = find_metric(reader, "cidx.embedding.requests")
    assert requests_metric is not None, "cidx.embedding.requests not emitted"
    dp = list(requests_metric.data.data_points)[0]
    assert dp.value == 1
    assert dp.attributes["model"] == model
    assert dp.attributes["status"] == "success"

    tokens_metric = find_metric(reader, "cidx.embedding.tokens")
    assert tokens_metric is not None
    tokens_dp = list(tokens_metric.data.data_points)[0]
    assert tokens_dp.value > 0, "real tokenizer must count > 0 tokens"

    duration_metric = find_metric(reader, "cidx.embedding.duration")
    assert duration_metric is not None
    assert len(list(duration_metric.data.data_points)) == 1


def _assert_error_metrics(reader) -> None:
    """Assert cidx.embedding.requests/tokens/duration all recorded once on
    the error path: status=error, tokens=0 (the error branch never invokes
    the tokenizer), duration still present."""
    requests_metric = find_metric(reader, "cidx.embedding.requests")
    assert requests_metric is not None
    dp = list(requests_metric.data.data_points)[0]
    assert dp.attributes["status"] == "error"

    tokens_metric = find_metric(reader, "cidx.embedding.tokens")
    assert tokens_metric is not None, (
        "cidx.embedding.tokens must still be recorded (0) on the error path"
    )
    tokens_dp = list(tokens_metric.data.data_points)[0]
    assert tokens_dp.value == 0

    duration_metric = find_metric(reader, "cidx.embedding.duration")
    assert duration_metric is not None, (
        "cidx.embedding.duration must still be recorded on the error path"
    )
    assert len(list(duration_metric.data.data_points)) == 1


class TestMakeSyncRequestEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics_with_model_attribute(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"data": [{"embedding": [0.1] * EMBEDDING_DIM}]}
            )

        config = VoyageAIConfig(model="voyage-code-3", max_retries=3, retry_delay=0.01)
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            client._make_sync_request(["hello world"], retry=False)
            _assert_success_metrics(reader, "voyage-code-3")

    def test_failure_records_embedding_metric_status_error(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        config = VoyageAIConfig(
            model="voyage-code-3",
            max_retries=0,
            retry_delay=0.01,
            exponential_backoff=False,
        )
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                client._make_sync_request(["hello"], retry=False)
            _assert_error_metrics(reader)

    def test_two_real_calls_each_increment_the_counter_no_client_side_caching(
        self, mock_api_key
    ):
        """VoyageAIClient itself has no cache -- two calls for the SAME text
        must each independently emit a metric. (The query-embedding cache
        that *would* short-circuit a repeat call lives one layer above, in
        server/services/governed_call.py, and is proven separately in
        tests/unit/server/services/test_governed_call*.py not to reach the
        provider at all on a hit -- this test proves the provider layer
        itself never suppresses repeat metrics.)
        """
        call_count: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            call_count.append(1)
            return httpx.Response(
                200, json={"data": [{"embedding": [0.1] * EMBEDDING_DIM}]}
            )

        config = VoyageAIConfig(model="voyage-code-3")
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            client._make_sync_request(["same text"], retry=False)
            client._make_sync_request(["same text"], retry=False)

            assert len(call_count) == 2
            requests_metric = find_metric(reader, "cidx.embedding.requests")
            assert requests_metric is not None
            dp = list(requests_metric.data.data_points)[0]
            assert dp.value == 2


class TestMakeSyncContextualizedRequestEmitsEmbeddingMetrics:
    def test_success_records_embedding_metrics(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "index": 0,
                            "data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIM}],
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

        with active_application_metrics_singleton() as (_metrics, reader):
            client._make_sync_contextualized_request(
                [["chunk one", "chunk two"]],
                input_type="document",
                output_dimension=EMBEDDING_DIM,
                retry=False,
            )
            _assert_success_metrics(reader, "voyage-context-4")

    def test_failure_records_embedding_metric_status_error(self, mock_api_key):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "server error"})

        config = VoyageAIConfig(
            model="voyage-context-4",
            max_retries=0,
            retry_delay=0.01,
        )
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            with pytest.raises(Exception):
                client._make_sync_contextualized_request(
                    [["chunk"]],
                    input_type="document",
                    output_dimension=EMBEDDING_DIM,
                    retry=False,
                )
            _assert_error_metrics(reader)
