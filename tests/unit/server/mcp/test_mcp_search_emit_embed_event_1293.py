"""Story #1293: MCP's two inline query-vector helpers must each call
emit_embed_event(meta) right after coalesced_query_embedding() returns, so
every needed embed on the MCP front door is recorded as a durable
search_embed_event row.

Mirrors the existing direct-call test pattern in test_search_event_context
instrumentation tests for _compute_memory_query_vector /
_compute_shared_query_vector.
"""

from unittest.mock import patch

from code_indexer.server.services.governed_call import EmbeddingCacheMetadata


DIRECT_META = EmbeddingCacheMetadata(
    key_found=False,
    cache_mode="on",
    provider_latency_ms=8,
    provider="voyage-ai",
    outcome="miss",
    role="direct",
)
FAKE_VEC = [0.1, 0.2, 0.3]


def _patch_http_factory():
    return patch(
        "code_indexer.server.services.search_service._get_http_client_factory",
        return_value=None,
    )


class TestComputeMemoryQueryVectorEmitsEmbedEvent:
    def test_calls_emit_embed_event_with_meta(self):
        import code_indexer.server.mcp.handlers.search as sh
        import code_indexer.server.services.governed_call as gc_mod

        with (
            patch.object(
                gc_mod,
                "coalesced_query_embedding",
                return_value=(FAKE_VEC, DIRECT_META),
            ),
            _patch_http_factory(),
            patch.object(sh, "emit_embed_event") as mock_emit,
        ):
            sh._compute_memory_query_vector("test query")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] is DIRECT_META


class TestComputeSharedQueryVectorEmitsEmbedEvent:
    def test_calls_emit_embed_event_with_meta(self):
        import code_indexer.server.mcp.handlers.search as sh
        import code_indexer.server.services.governed_call as gc_mod

        with (
            patch.object(
                gc_mod,
                "coalesced_query_embedding",
                return_value=(FAKE_VEC, DIRECT_META),
            ),
            _patch_http_factory(),
            patch.object(sh, "emit_embed_event") as mock_emit,
        ):
            sh._compute_shared_query_vector("test query")

        mock_emit.assert_called_once()
        assert mock_emit.call_args[0][0] is DIRECT_META
