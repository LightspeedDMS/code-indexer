"""Story #1586 Finding 7: prove a query-embedding-cache HIT does not
double-count cidx.embedding.requests.

test_voyage_ai_embedding_metrics_wiring_1586.py's
test_two_real_calls_each_increment_the_counter_no_client_side_caching proves
the INVERSE case: VoyageAIClient itself has no cache, so two direct calls
each increment the counter. This test closes the complementary gap the
code-review finding identified: driving coalesced_query_embedding() (the
real cache-wrapper entry point every server query site calls) TWICE with
the SAME text, through a REAL SQLite-backed QueryEmbeddingCache in "on"
mode, must increment cidx.embedding.requests exactly ONCE -- the second
call is a cache HIT and must never reach the provider's real HTTP
boundary at all.

Real components throughout (MESSI Rule #1: no mocks of the code under
test): real QueryEmbeddingCache + real QueryEmbeddingCacheSqliteBackend,
real VoyageAIClient wired to a fake HTTP transport (matching the
established pattern in test_voyage_ai_embedding_metrics_wiring_1586.py),
real ApplicationMetrics + real InMemoryMetricReader. No coalescer registry
is installed, so coalesced_query_embedding takes the direct Path B
(_serve_with_cache -> governed_query_embedding -> provider.get_embedding),
matching the exact recipe already established in
test_query_embedding_cache_wrap_1105.py's TestAnchorTokenDialThroughWrap.
"""

from __future__ import annotations

import os
from typing import List
from unittest.mock import patch

import httpx
import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.voyage_ai import VoyageAIClient
from code_indexer.server.services import governed_call
from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache
from code_indexer.server.storage.sqlite_backends import (
    QueryEmbeddingCacheSqliteBackend,
)

from tests.unit.server.telemetry.otel_test_support import (
    active_application_metrics_singleton,
    find_metric,
)

EMBEDDING_DIM = 1024
TEST_QUERY_TEXT = "find authentication middleware"


class _FakeSyncClientFactory:
    """Minimal SyncClientFactory returning an httpx.Client wired to a
    MockTransport -- a real client, real request/response cycle, fake
    network layer only (same pattern as
    test_voyage_ai_embedding_metrics_wiring_1586.py). The returned client
    is closed by the code under test's own `with _client_ctx as client:`
    block (voyage_ai.py's _do_post_and_validate) -- no separate cleanup
    is needed here, matching the established sibling test file exactly.
    """

    def __init__(self, handler) -> None:
        self._handler = handler

    def create_sync_client(self, *, transport=None, pooled: bool = False, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(self._handler))


@pytest.fixture
def mock_api_key():
    with patch.dict(os.environ, {"VOYAGE_API_KEY": "PLACEHOLDER"}):
        yield "PLACEHOLDER"


@pytest.fixture(autouse=True)
def _reset_registry():
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.services.coalescer_registry import (
        clear_coalescer_registry,
    )

    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()
    reset_config_service()
    yield
    clear_coalescer_registry()
    governed_call.clear_query_embedding_cache()
    reset_config_service()


def _make_real_on_mode_cache(tmp_path) -> QueryEmbeddingCache:
    """Real SQLite-backed QueryEmbeddingCache forced to "on" mode, matching
    test_query_embedding_cache_wrap_1105.py's _make_real_cache recipe."""
    backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
    cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")
    # The live config service defaults voyage_mode to "shadow" (which would
    # prevent the HIT short-circuit this test needs) -- override mode_for
    # directly so the test is independent of live config service wiring,
    # exactly as test_query_embedding_cache_wrap_1105.py's _make_real_cache
    # already documents and does for the same reason.
    cache.mode_for = lambda provider_name: "on"  # type: ignore[method-assign]
    return cache


def _assert_requests_metric_value(reader, expected: int) -> None:
    """Assert cidx.embedding.requests' single data point equals `expected`."""
    requests_metric = find_metric(reader, "cidx.embedding.requests")
    assert requests_metric is not None
    dp = list(requests_metric.data.data_points)[0]
    assert dp.value == expected, (
        f"cidx.embedding.requests expected {expected}, got {dp.value}"
    )


class TestCoalescedQueryEmbeddingCacheHitDoesNotDoubleCountMetrics:
    def test_second_identical_call_is_cache_hit_and_does_not_double_count(
        self, monkeypatch, tmp_path, mock_api_key
    ):
        real_http_call_count: List[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            real_http_call_count.append(1)
            return httpx.Response(
                200, json={"data": [{"embedding": [0.1] * EMBEDDING_DIM}]}
            )

        config = VoyageAIConfig(model="voyage-code-3", max_retries=3, retry_delay=0.01)
        client = VoyageAIClient(
            config, http_client_factory=_FakeSyncClientFactory(handler)
        )

        cache = _make_real_on_mode_cache(tmp_path)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
        # No coalescer registry installed -> Path B (direct governed call),
        # matching test_query_embedding_cache_wrap_1105.py's real-cache recipe.
        monkeypatch.setattr(governed_call, "get_coalescer_registry", lambda: None)

        with active_application_metrics_singleton() as (_metrics, reader):
            governed_call.coalesced_query_embedding(client, TEST_QUERY_TEXT)
            assert len(real_http_call_count) == 1, "first call must be a cache MISS"
            _assert_requests_metric_value(reader, 1)

            governed_call.coalesced_query_embedding(client, TEST_QUERY_TEXT)
            assert len(real_http_call_count) == 1, (
                "second identical call must be a cache HIT -- it must never "
                "reach the provider's real HTTP boundary"
            )
            _assert_requests_metric_value(reader, 1)
