"""Story #1293: search_service.py's "Backend" (non-FSV, direct/inline) branch
must call emit_embed_event(meta) right after coalesced_query_embedding()
returns, so every needed embed on this inline path is recorded as a durable
search_embed_event row.

Mirrors the exact test-harness pattern already established for the
meta->ctx wiring at
tests/unit/server/services/test_search_event_instrumentation.py::
test_search_service_backend_path_writes_embed_meta_to_ctx.
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.server.services.search_event_context import (
    SearchEventContext,
    _search_event_ctx,
)


def test_backend_path_calls_emit_embed_event_with_meta():
    from code_indexer.server.services.search_service import SemanticSearchService
    import code_indexer.server.services.search_service as ss_mod
    import code_indexer.server.services.governed_call as gc_mod

    DIRECT_META = EmbeddingCacheMetadata(
        key_found=False,
        cache_mode="on",
        provider_latency_ms=10,
        provider="voyage-ai",
        outcome="miss",
        role="direct",
    )
    FAKE_VEC = [0.1, 0.2, 0.3]

    mock_vsc = MagicMock()
    mock_vsc.resolve_collection_name.return_value = "test_coll"
    mock_vsc.search.return_value = []
    mock_backend = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vsc

    ctx = SearchEventContext(
        username="alice", repo_alias="r", search_type="semantic", query_text="q"
    )
    token = _search_event_ctx.set(ctx)
    try:
        with (
            patch.object(
                gc_mod,
                "coalesced_query_embedding",
                return_value=(FAKE_VEC, DIRECT_META),
            ),
            patch.object(
                ss_mod,
                "_load_repo_config",
                return_value={"embedding_provider": "voyage-ai"},
            ),
            patch(
                "code_indexer.server.services.search_service.BackendFactory"
            ) as mock_bf,
            patch(
                "code_indexer.server.services.search_service.EmbeddingProviderFactory"
            ) as mock_epf,
            patch.object(ss_mod, "emit_embed_event") as mock_emit,
        ):
            mock_bf.create.return_value = mock_backend
            mock_epf.create.return_value = MagicMock()
            SemanticSearchService()._perform_semantic_search(
                "/fake/repo", "q", 5, False
            )
    finally:
        _search_event_ctx.reset(token)

    mock_emit.assert_called_once()
    called_meta = mock_emit.call_args[0][0]
    assert called_meta is DIRECT_META
