"""Bug #669 multi-provider parallel timeout handling -- REMOVED by Story #1291 AC9.

Bug #669 originally covered `as_completed()` TimeoutError handling for the
MULTI-PROVIDER PARALLEL fan-out branch of `execute_temporal_query_with_fusion`
(multiple DIFFERENT embedders queried concurrently, then RRF-fused together).

Story #1291 AC9 makes cross-embedder fusion structurally forbidden: query
discovery (`_discover_provider_shards_with_pruning`) now resolves to AT MOST
ONE embedder per query (the explicit `temporal_embedder` override, or
`config.temporal.active_embedder` when omitted) -- so the multi-provider
parallel fan-out branch (ThreadPoolExecutor + `as_completed(timeout=...)`)
that Bug #669 patched no longer exists. There is nothing left to time out in
parallel, because there is never more than one embedder being queried.

The single-provider path (querying one embedder's own quarterly shards
sequentially) is UNCHANGED by this removal -- it never had a wall-clock
timeout wrapping it before Story #1291 either. `TEMPORAL_QUERY_TIMEOUT_SECONDS`
was deleted entirely by Issue #1398 (confirmed dead code with zero real
consumers) rather than kept around as a misleading unused knob -- see
test_temporal_query_timeout_seconds_removed_1398.py for the proof that it
no longer exists in source.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.config import VoyageAIConfig
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


def test_single_provider_query_returns_promptly_with_no_spurious_timeout_warning(
    tmp_path,
):
    """Regression: the single-provider path (the only path since AC9) has no
    wall-clock timeout and must not emit a false 'timed out' warning."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

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
        mock_instance = MagicMock()
        mock_instance.query_temporal.return_value = _make_results_with(
            [_make_result("a.py")]
        )
        MockService.return_value = mock_instance
        MockFactory.create.return_value = MagicMock()

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="test query",
            limit=5,
            time_range=None,
        )

    assert isinstance(result, TemporalSearchResults)
    assert len(result.results) == 1
    assert not result.warning
