"""Story #1293 S1b [A5]: temporal compute-once reuse seam.

Before this fix, execute_temporal_query_with_fusion() re-embedded the SAME
query text once per sequential shard (1 miss + N-1 phantom warm hits within
a single request -- the same intra-request phantom-hit bug that omni's
precomputed-vector seam already solves). This mirrors that omni approach:
compute the query embedding ONCE (via coalesced_query_embedding, emitting
exactly one search_embed_event row for the (provider, model, config_digest)
resolved for the query's single embedder), then pass it down as
precomputed_query_vector to every per-shard query_temporal() call so FSV/
non-FSV embedding is skipped entirely per shard.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from code_indexer.config import VoyageAIConfig
from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)


def _make_result(file_path: str = "foo.py", score: float = 0.9) -> TemporalSearchResult:
    return TemporalSearchResult(
        file_path=file_path,
        chunk_index=0,
        content="content",
        score=score,
        metadata={},
        temporal_context={"commit_hash": "abc123"},
    )


def _make_results_with(results, query: str = "test") -> TemporalSearchResults:
    return TemporalSearchResults(
        results=results,
        query=query,
        filter_type="none",
        filter_value=None,
        total_found=len(results),
    )


def _make_mock_config():
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai = VoyageAIConfig(model="voyage-code-3")
    config.temporal.embedders = ["voyage-code-3"]
    config.temporal.active_embedder = "voyage-code-3"
    return config


def _make_mock_vector_store(project_root: Path):
    vs = MagicMock()
    vs.project_root = project_root
    return vs


def test_reuse_seam_computes_embedding_once_across_shards(tmp_path):
    """Two sequential shards of ONE embedder must share ONE precomputed vector
    -- coalesced_query_embedding is called exactly ONCE, not once per shard."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    r1 = _make_result("q1.py")
    r2 = _make_result("q2.py")
    results_q1 = _make_results_with([r1])
    results_q2 = _make_results_with([r2])

    two_shards = [
        (
            "code-indexer-temporal-voyage_code_3",
            [
                "code-indexer-temporal-voyage_code_3-2025Q1",
                "code-indexer-temporal-voyage_code_3-2025Q2",
            ],
        )
    ]

    fake_vec = [0.1, 0.2, 0.3]
    fake_meta = EmbeddingCacheMetadata(
        outcome="miss", role="direct", provider="voyage-ai"
    )

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=two_shards,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
            side_effect=lambda cols: (cols, []),
        ),
        patch(
            "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
        ) as MockService,
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._build_query_provider_for_embedder",
            return_value=MagicMock(),
        ) as mock_build_provider,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.coalesced_query_embedding",
            return_value=(fake_vec, fake_meta),
        ) as mock_embed,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.emit_embed_event"
        ) as mock_emit,
    ):
        mock_service_instance = MagicMock()
        mock_service_instance.query_temporal.side_effect = [results_q1, results_q2]
        MockService.return_value = mock_service_instance
        MockFactory.create.return_value = MagicMock()

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="auth logic",
            limit=5,
        )

    # The up-front reuse-seam provider is built with the resolved embedder
    # name (per-shard provider construction for TemporalSearchService's own
    # constructor is unrelated pre-existing behavior -- not asserted here).
    assert (
        call(config, "code-indexer-temporal-voyage_code_3")
        in mock_build_provider.call_args_list
    )
    # Exactly ONE embed call for the whole request, regardless of shard count.
    mock_embed.assert_called_once()
    mock_emit.assert_called_once_with(fake_meta)

    # Both shards queried, each reusing the SAME precomputed vector.
    assert mock_service_instance.query_temporal.call_count == 2
    for _call in mock_service_instance.query_temporal.call_args_list:
        assert _call.kwargs.get("precomputed_query_vector") == fake_vec

    assert len(result.results) >= 1


def test_reuse_seam_falls_back_to_per_shard_embed_on_precompute_failure(tmp_path):
    """If the up-front embed fails, fall back EXPLICITLY to per-shard embedding
    (precomputed_query_vector=None) -- never silently drop the query."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    result_item = _make_result("service.py")
    service_results = _make_results_with([result_item])

    one_shard = [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3-2025Q1"],
        )
    ]

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=one_shard,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
            side_effect=lambda cols: (cols, []),
        ),
        patch(
            "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
        ) as MockService,
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._build_query_provider_for_embedder",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.coalesced_query_embedding",
            side_effect=RuntimeError("governor busy"),
        ),
    ):
        mock_service_instance = MagicMock()
        mock_service_instance.query_temporal.return_value = service_results
        MockService.return_value = mock_service_instance
        MockFactory.create.return_value = MagicMock()

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="query",
            limit=5,
        )

    mock_service_instance.query_temporal.assert_called_once()
    call = mock_service_instance.query_temporal.call_args
    assert call.kwargs.get("precomputed_query_vector") is None
    assert len(result.results) >= 1
