"""Tests proving SearchTimeoutsConfig.embedding_provider_timeout_seconds and
.reranker_timeout_seconds actually reach the constructed HTTP client
(Issue #1398), via a REAL config object read through get_config_service()
-- not a hand-built VoyageAIConfig/timeout kwarg that would bypass the
read path this issue is about wiring up.
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.config_service import (
    ConfigService,
    set_config_service,
    reset_config_service,
)


@pytest.fixture
def isolated_config_service(tmp_path):
    svc = ConfigService(server_dir_path=str(tmp_path))
    set_config_service(svc)
    try:
        yield svc
    finally:
        reset_config_service()


# ---------------------------------------------------------------------------
# Embedding provider timeout wiring (mcp/handlers/search.py)
# ---------------------------------------------------------------------------


class TestEmbeddingProviderTimeoutReachesVoyageAIClient:
    """_compute_memory_query_vector / _compute_shared_query_vector construct
    VoyageAIClient(VoyageAIConfig()) for the server-side query-embedding
    path. Proves the configured embedding_provider_timeout_seconds value
    actually reaches the VoyageAIConfig passed into the client constructor."""

    def test_compute_memory_query_vector_passes_configured_timeout(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "embedding_provider_timeout_seconds", 77
        )

        from code_indexer.server.mcp.handlers.search import (
            _compute_memory_query_vector,
        )

        with (
            patch("code_indexer.services.voyage_ai.VoyageAIClient") as MockClient,
            patch(
                "code_indexer.server.services.governed_call.coalesced_query_embedding"
            ) as mock_embed,
        ):
            mock_embed.return_value = (
                [0.1, 0.2],
                MagicMock(key_found=False, cache_mode="live", provider_latency_ms=1.0),
            )
            _compute_memory_query_vector("hello world")

        MockClient.assert_called_once()
        constructed_config = MockClient.call_args[0][0]
        assert constructed_config.timeout == 77, (
            f"Expected VoyageAIConfig.timeout=77 (configured value), got "
            f"{constructed_config.timeout} -- embedding_provider_timeout_seconds "
            f"is not reaching the constructed client"
        )

    def test_compute_shared_query_vector_passes_configured_timeout(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "embedding_provider_timeout_seconds", 55
        )

        from code_indexer.server.mcp.handlers.search import (
            _compute_shared_query_vector,
        )

        with (
            patch("code_indexer.services.voyage_ai.VoyageAIClient") as MockClient,
            patch(
                "code_indexer.server.services.governed_call.coalesced_query_embedding"
            ) as mock_embed,
            patch(
                "code_indexer.server.services.coalescer_registry._digest_for_provider",
                return_value="fallback-no-config",
            ),
        ):
            mock_embed.return_value = (
                [0.3, 0.4],
                MagicMock(key_found=False, cache_mode="live", provider_latency_ms=1.0),
            )
            _compute_shared_query_vector("another query")

        MockClient.assert_called_once()
        constructed_config = MockClient.call_args[0][0]
        assert constructed_config.timeout == 55

    def test_default_embedding_timeout_matches_pre_1398_hardcoded_value(
        self, isolated_config_service
    ) -> None:
        """Byte-identical default (30s) preserves pre-#1398 behavior."""
        from code_indexer.server.mcp.handlers.search import (
            _compute_memory_query_vector,
        )

        with (
            patch("code_indexer.services.voyage_ai.VoyageAIClient") as MockClient,
            patch(
                "code_indexer.server.services.governed_call.coalesced_query_embedding"
            ) as mock_embed,
        ):
            mock_embed.return_value = (
                [0.1],
                MagicMock(key_found=False, cache_mode="live", provider_latency_ms=1.0),
            )
            _compute_memory_query_vector("q")

        constructed_config = MockClient.call_args[0][0]
        assert constructed_config.timeout == 30


class TestConfiguredEmbeddingTimeoutDefensiveAgainstMinimalConfigDoubles:
    """Regression: many existing unit tests (e.g.
    test_no_embedding_cache_shortcut_1108.py) stub get_config_service() with
    a minimal hand-built fake config object exposing ONLY the attributes the
    code under test is documented to need (e.g. just memory_retrieval_config)
    -- NOT a real ServerConfig, so it has no search_timeouts_config attribute
    AT ALL (not even None). _configured_embedding_timeout_seconds() must use
    getattr() defensively (matching _configured_reranker_timeout_seconds's
    existing pattern in reranking.py) rather than direct attribute access,
    or it raises AttributeError on every such fake -- silently caught by the
    broad except-Exception in _compute_memory_query_vector /
    _compute_shared_query_vector, which then skips the embedding call
    entirely and breaks unrelated tests asserting the embedding WAS called.
    """

    def test_returns_default_for_config_object_missing_the_attribute_entirely(
        self,
    ) -> None:
        from code_indexer.server.mcp.handlers import search as search_handler

        class _FakeCfgWithoutSearchTimeouts:
            pass

        class _FakeConfigService:
            def get_config(self):
                return _FakeCfgWithoutSearchTimeouts()

        original_get_config_service = search_handler.get_config_service
        search_handler.get_config_service = lambda: _FakeConfigService()
        try:
            timeout = search_handler._configured_embedding_timeout_seconds()
        finally:
            search_handler.get_config_service = original_get_config_service

        assert timeout == 30


# ---------------------------------------------------------------------------
# Reranker timeout wiring (server/mcp/reranking.py)
# ---------------------------------------------------------------------------


class TestRerankerTimeoutReachesClientConstruction:
    """_attempt_provider_rerank constructs client_cls(...) with no timeout
    override today -- both VoyageRerankerClient and CohereRerankerClient
    fall back to their class-level default of 15.0. Proves the configured
    reranker_timeout_seconds value now reaches that construction."""

    def test_attempt_provider_rerank_passes_configured_timeout_to_client(
        self, isolated_config_service
    ) -> None:
        isolated_config_service.update_setting(
            "search_timeouts", "reranker_timeout_seconds", 33
        )

        from code_indexer.server.mcp.reranking import (
            _attempt_provider_rerank,
            _configured_reranker_timeout_seconds,
        )

        rerank_result = MagicMock()
        rerank_result.index = 0
        rerank_result.relevance_score = 0.9

        with (
            patch(
                "code_indexer.server.mcp.reranking.VoyageRerankerClient"
            ) as MockVoyage,
            patch(
                "code_indexer.server.mcp.reranking.ProviderHealthMonitor"
            ) as MockMonitor,
        ):
            monitor_inst = MockMonitor.get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False
            MockVoyage.return_value.rerank.return_value = [rerank_result]

            _attempt_provider_rerank(
                provider_name="Voyage",
                health_key="voyage-reranker",
                client_cls=MockVoyage,
                query="test",
                documents=["doc1"],
                instruction=None,
                top_k=1,
                monitor=monitor_inst,
                timeout_seconds=_configured_reranker_timeout_seconds(
                    isolated_config_service
                ),
            )

        MockVoyage.assert_called_once()
        assert MockVoyage.call_args.kwargs.get("timeout") == 33

    def test_default_reranker_timeout_matches_pre_1398_hardcoded_value(
        self, isolated_config_service
    ) -> None:
        from code_indexer.server.mcp.reranking import (
            _configured_reranker_timeout_seconds,
        )

        assert _configured_reranker_timeout_seconds(isolated_config_service) == 15.0

    def test_apply_reranking_sync_threads_configured_timeout_through(
        self, isolated_config_service
    ) -> None:
        """End-to-end within reranking.py: _apply_reranking_sync ->
        _run_provider_chain -> _attempt_provider_rerank must carry the
        configured value all the way to client construction, using the
        REAL config_service (not a hand-built provider bypassing the
        read path)."""
        isolated_config_service.update_setting(
            "search_timeouts", "reranker_timeout_seconds", 44
        )
        # Non-empty voyage_reranker_model so the reranker isn't
        # short-circuited as "disabled" before _run_provider_chain runs.
        isolated_config_service.update_setting(
            "rerank", "voyage_reranker_model", "rerank-2.5"
        )

        from code_indexer.server.mcp import reranking as reranking_module

        rerank_result = MagicMock()
        rerank_result.index = 0
        rerank_result.relevance_score = 0.9

        with (
            patch.object(reranking_module, "VoyageRerankerClient") as MockVoyage,
            patch.object(
                reranking_module.ProviderHealthMonitor, "get_instance"
            ) as mock_get_instance,
        ):
            monitor_inst = mock_get_instance.return_value
            monitor_inst.get_health.return_value = {}
            monitor_inst.is_sinbinned.return_value = False
            MockVoyage.return_value.rerank.return_value = [rerank_result]

            results, meta = reranking_module._apply_reranking_sync(
                results=[{"payload": {"content": "doc1"}}],
                rerank_query="test query",
                rerank_instruction=None,
                content_extractor=lambda r: r["payload"]["content"],
                requested_limit=1,
                config_service=isolated_config_service,
            )

        MockVoyage.assert_called_once()
        assert MockVoyage.call_args.kwargs.get("timeout") == 44


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
