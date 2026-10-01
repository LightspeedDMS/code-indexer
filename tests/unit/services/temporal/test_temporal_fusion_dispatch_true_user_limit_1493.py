"""Tests for Story #1493 AC1 dispatch-layer wiring: the ORIGINAL
user-requested `limit` (before the shard-level TEMPORAL_OVERFETCH_MULTIPLIER
is applied) must reach TemporalSearchService.query_temporal() as
`true_user_limit`, so query_temporal can bound the COMBINED overfetch
multiplier against the real user request rather than the already-multiplied
per-shard value.
"""

from unittest.mock import MagicMock, patch

from code_indexer.config import VoyageAIConfig
from code_indexer.services.temporal.temporal_fusion import (
    TEMPORAL_OVERFETCH_MULTIPLIER,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)

_USER_REQUESTED_LIMIT = 10


def _make_mock_config():
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai = VoyageAIConfig(model="voyage-code-3")
    config.temporal.embedders = ["voyage-code-3"]
    config.temporal.active_embedder = "voyage-code-3"
    return config


def _make_results_with(results, query: str = "test") -> TemporalSearchResults:
    return TemporalSearchResults(
        results=results,
        query=query,
        filter_type="none",
        filter_value=None,
        total_found=len(results),
    )


def test_true_user_limit_reaches_query_temporal(tmp_path):
    """The original (pre-shard-multiplied) user limit must be forwarded to
    query_temporal() as true_user_limit -- not the already-multiplied
    per-shard limit, which must remain exactly
    true_user_limit * TEMPORAL_OVERFETCH_MULTIPLIER (byte-identical to
    pre-#1493 shard-level behavior; #1493 changes only what query_temporal
    does with that pre-multiplied value internally)."""
    config = _make_mock_config()
    vector_store = MagicMock()
    vector_store.project_root = tmp_path
    vector_store.memory_governor = None

    expected_result = TemporalSearchResult(
        file_path="auth.py",
        chunk_index=0,
        content="content",
        score=0.9,
        metadata={},
        temporal_context={"commit_hash": "abc123"},
    )
    expected_results = _make_results_with([expected_result])

    one_provider = [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3"],
        )
    ]

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=one_provider,
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
    ):
        mock_service_instance = MagicMock()
        mock_service_instance.query_temporal.return_value = expected_results
        MockService.return_value = mock_service_instance
        MockFactory.create.return_value = MagicMock()

        execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="auth logic",
            limit=_USER_REQUESTED_LIMIT,
        )

    mock_service_instance.query_temporal.assert_called_once()
    call_kwargs = mock_service_instance.query_temporal.call_args.kwargs
    assert call_kwargs["true_user_limit"] == _USER_REQUESTED_LIMIT
    assert call_kwargs["limit"] == _USER_REQUESTED_LIMIT * TEMPORAL_OVERFETCH_MULTIPLIER
