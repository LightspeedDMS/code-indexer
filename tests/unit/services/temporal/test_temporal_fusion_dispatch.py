"""Tests for temporal fusion dispatch (Story #634).

Covers:
- execute_temporal_query_with_fusion() with no collections, single, and multi-provider
- _query_single_provider() attribution fields populated
- fuse_rrf_multi wired correctly for multi-provider path
- TEMPORAL_QUERY_TIMEOUT_SECONDS constant value
- _make_config_manager() shim wraps config correctly
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion_dispatch import (
    TEMPORAL_QUERY_TIMEOUT_SECONDS,
    _make_config_manager,
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    return config


def _make_mock_vector_store(project_root: Path):
    vs = MagicMock()
    vs.project_root = project_root
    return vs


# ---------------------------------------------------------------------------
# test_timeout_constant_defined
# ---------------------------------------------------------------------------


def test_timeout_constant_defined():
    """TEMPORAL_QUERY_TIMEOUT_SECONDS must equal 15."""
    assert TEMPORAL_QUERY_TIMEOUT_SECONDS == 15


# ---------------------------------------------------------------------------
# test_make_config_manager_shim
# ---------------------------------------------------------------------------


def test_make_config_manager_shim():
    """_make_config_manager shim must return the wrapped config via get_config()."""
    config = _make_mock_config()
    manager = _make_config_manager(config)
    assert manager.get_config() is config


# ---------------------------------------------------------------------------
# test_execute_returns_empty_when_no_collections
# ---------------------------------------------------------------------------


def test_execute_returns_empty_when_no_collections(tmp_path):
    """No temporal dirs on disk → empty results with warning message."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    index_path = tmp_path / "index"
    index_path.mkdir()

    result = execute_temporal_query_with_fusion(
        config=config,
        index_path=index_path,
        vector_store=vector_store,
        query_text="some query",
        limit=10,
    )

    assert result.results == []
    assert result.warning is not None
    assert len(result.warning) > 0


# ---------------------------------------------------------------------------
# test_zero_providers_returns_warning
# ---------------------------------------------------------------------------


def test_zero_providers_returns_warning(tmp_path):
    """Nonexistent index path → result has a warning and empty results list."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    index_path = tmp_path / "nonexistent_index"

    result = execute_temporal_query_with_fusion(
        config=config,
        index_path=index_path,
        vector_store=vector_store,
        query_text="search term",
        limit=5,
    )

    assert result.results == []
    assert result.warning is not None


# ---------------------------------------------------------------------------
# test_single_provider_no_fusion_overhead
# ---------------------------------------------------------------------------


def test_single_provider_no_fusion_overhead(tmp_path):
    """One provider group → TemporalSearchService queried once for the shard."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    expected_result = _make_result("auth.py")
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

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="auth logic",
            limit=5,
        )

    # Single provider: results must contain the expected file
    assert len(result.results) >= 1
    assert any(r.file_path == "auth.py" for r in result.results)
    mock_service_instance.query_temporal.assert_called_once()


# ---------------------------------------------------------------------------
# test_single_provider_attribution_populated
# ---------------------------------------------------------------------------


def test_single_provider_attribution_populated(tmp_path):
    """Single provider query must set source_provider, contributing_providers, fusion_score."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    result_item = _make_result("service.py", score=0.85)
    service_results = _make_results_with([result_item])

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

    assert len(result.results) >= 1
    r = result.results[0]
    assert r.source_provider is not None
    assert r.contributing_providers is not None
    assert len(r.contributing_providers) == 1
    assert r.fusion_score == r.score


# ---------------------------------------------------------------------------
# test_multi_provider_dispatches_to_all
# ---------------------------------------------------------------------------


def test_multi_provider_dispatches_to_all(tmp_path):
    """Two provider groups → both are queried (TemporalSearchService called twice)."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    result_a = _make_result("a.py", score=0.9)
    result_b = _make_result("b.py", score=0.8)

    call_count = []  # type: ignore[var-annotated]

    two_providers = [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3"],
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            ["code-indexer-temporal-embed_v4_0"],
        ),
    ]

    def make_service(*args, **kwargs):
        instance = MagicMock()
        if len(call_count) == 0:
            instance.query_temporal.return_value = _make_results_with([result_a])
        else:
            instance.query_temporal.return_value = _make_results_with([result_b])
        call_count.append(1)
        return instance

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=two_providers,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
            side_effect=lambda cols: (cols, []),
        ),
        patch(
            "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService",
            side_effect=make_service,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
    ):
        MockFactory.create.return_value = MagicMock()

        execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="query",
            limit=5,
        )

    assert len(call_count) == 2


# ---------------------------------------------------------------------------
# test_multi_provider_fusion_applied
# ---------------------------------------------------------------------------


def test_multi_provider_fusion_applied(tmp_path):
    """Two providers → fuse_rrf_multi called and results are fused."""
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    result_a = _make_result("a.py", score=0.9)
    result_b = _make_result("b.py", score=0.8)

    two_providers = [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3"],
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            ["code-indexer-temporal-embed_v4_0"],
        ),
    ]

    def make_service(*args, **kwargs):
        instance = MagicMock()
        instance.query_temporal.return_value = _make_results_with([result_a, result_b])
        return instance

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=two_providers,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
            side_effect=lambda cols: (cols, []),
        ),
        patch(
            "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService",
            side_effect=make_service,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.fuse_rrf_multi"
        ) as mock_fuse,
    ):
        MockFactory.create.return_value = MagicMock()
        mock_fuse.return_value = [result_a, result_b]

        execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="query",
            limit=5,
        )

    mock_fuse.assert_called_once()
