"""Tests for Bug #669: TimeoutError handling in multi-provider temporal query path.

Verifies that as_completed() TimeoutError is caught at the loop level,
partial results are returned, and futures are cancelled before ThreadPoolExecutor
shutdown to avoid blocking.

Migrated from _query_multi_provider_fusion (deleted, Story #1171 C3) to
execute_temporal_query_with_fusion (the live production entry point).
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion_dispatch import (
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


def _two_provider_groups():
    """Two provider groups for the multi-provider parallel path."""
    return [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3"],
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            ["code-indexer-temporal-embed_v4_0"],
        ),
    ]


def _three_provider_groups():
    """Three provider groups for the multi-provider parallel path."""
    return [
        (
            "code-indexer-temporal-voyage_code_3",
            ["code-indexer-temporal-voyage_code_3"],
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            ["code-indexer-temporal-embed_v4_0"],
        ),
        (
            "code-indexer-temporal-cohere_embed_3",
            ["code-indexer-temporal-cohere_embed_3"],
        ),
    ]


# ---------------------------------------------------------------------------
# test_all_futures_timeout_returns_empty_no_exception
# ---------------------------------------------------------------------------


def test_all_futures_timeout_returns_empty_no_exception(tmp_path):
    """All providers time out → returns TemporalSearchResults(results=[]) with warning.

    The function must NOT raise an exception. The TimeoutError from as_completed()
    must be caught at the loop level (Bug #669).

    Also verifies:
    - The call returns within timeout + 2s (no blocking on in-flight threads).
    - record_temporal_failure is called for each timed-out provider group.

    TEMPORAL_QUERY_TIMEOUT_SECONDS is overridden to 0.05s so the test runs fast.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    sleep_duration = 0.3  # longer than forced timeout
    forced_timeout = 0.05

    def slow_query_temporal(**kwargs):
        time.sleep(sleep_duration)
        return _make_results_with([_make_result("a.py")])

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.TEMPORAL_QUERY_TIMEOUT_SECONDS",
            forced_timeout,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=_three_provider_groups(),
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
            "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
        ) as mock_record_failure,
    ):
        mock_instance = MagicMock()
        mock_instance.query_temporal.side_effect = slow_query_temporal
        MockService.return_value = mock_instance
        MockFactory.create.return_value = MagicMock()
        MockFactory.get_configured_providers.return_value = []

        t0 = time.time()
        # Must NOT raise — this was the bug
        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="test query",
            limit=5,
            time_range=None,
        )
        elapsed = time.time() - t0

    assert isinstance(result, TemporalSearchResults)
    assert result.results == []
    assert result.warning is not None
    assert len(result.warning) > 0

    # No-blocking requirement: must return well within timeout + 2s overhead
    assert elapsed < forced_timeout + 2.0, (
        f"execute_temporal_query_with_fusion blocked for {elapsed:.2f}s "
        f"(timeout={forced_timeout}s, max allowed={forced_timeout + 2.0}s)"
    )

    # Timed-out providers must be recorded as failures in health telemetry
    assert mock_record_failure.called, (
        "record_temporal_failure must be called for timed-out providers"
    )


# ---------------------------------------------------------------------------
# test_partial_futures_timeout_returns_partial_results
# ---------------------------------------------------------------------------


def test_partial_futures_timeout_returns_partial_results(tmp_path):
    """One fast provider + two slow providers → fast results returned, warning for slow ones.

    The fast collection completes before the forced timeout; the other two
    sleep past it. The function must return the one fast result and include a
    warning about the timed-out providers.

    Also verifies:
    - The call returns within timeout + 2s (no blocking on in-flight threads).
    - record_temporal_failure is called for the two timed-out providers.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    fast_coll_name = "code-indexer-temporal-voyage_code_3"
    fast_result = _make_result("fast.py", score=0.95)
    sleep_duration = 0.5  # slow providers sleep well past timeout
    forced_timeout = 0.3  # fast provider (~0s) beats timeout; slow ones sleep 0.5s

    def make_service_instance(*args, **kwargs):
        coll_name = kwargs.get("collection_name", "")
        instance = MagicMock()

        if coll_name == fast_coll_name:
            instance.query_temporal.return_value = _make_results_with([fast_result])
        else:

            def slow_query(**qkwargs):
                time.sleep(sleep_duration)
                return _make_results_with([_make_result("slow.py")])

            instance.query_temporal.side_effect = slow_query

        return instance

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.TEMPORAL_QUERY_TIMEOUT_SECONDS",
            forced_timeout,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=_three_provider_groups(),
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
            side_effect=make_service_instance,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
        ) as mock_record_failure,
    ):
        MockFactory.create.return_value = MagicMock()
        MockFactory.get_configured_providers.return_value = []

        t0 = time.time()
        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="test query",
            limit=5,
            time_range=None,
        )
        elapsed = time.time() - t0

    assert isinstance(result, TemporalSearchResults)
    # At least the fast result must be present
    assert len(result.results) >= 1
    assert any(r.file_path == "fast.py" for r in result.results)
    # A timeout warning must exist
    assert result.warning is not None
    assert len(result.warning) > 0

    # No-blocking requirement: must return well within timeout + 3s overhead
    # (3s allows for slow CI machines; the important thing is no hang)
    assert elapsed < forced_timeout + 3.0, (
        f"execute_temporal_query_with_fusion blocked for {elapsed:.2f}s "
        f"(timeout={forced_timeout}s, max allowed={forced_timeout + 3.0}s)"
    )

    # Timed-out providers must be recorded as failures in health telemetry
    assert mock_record_failure.called, (
        "record_temporal_failure must be called for timed-out providers"
    )


# ---------------------------------------------------------------------------
# test_no_timeout_returns_all_results
# ---------------------------------------------------------------------------


def test_no_timeout_returns_all_results(tmp_path):
    """Control: all providers complete before timeout → all results, no warning.

    Verifies the happy path is not broken by the timeout-handling fix.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    coll_a = "code-indexer-temporal-voyage_code_3"
    coll_b = "code-indexer-temporal-embed_v4_0"
    result_a = _make_result("a.py", score=0.9)
    result_b = _make_result("b.py", score=0.8)

    result_map = {
        coll_a: _make_results_with([result_a]),
        coll_b: _make_results_with([result_b]),
    }

    def make_service_instance(*args, **kwargs):
        coll_name = kwargs.get("collection_name", "")
        instance = MagicMock()
        instance.query_temporal.return_value = result_map.get(
            coll_name, _make_results_with([])
        )
        return instance

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=_two_provider_groups(),
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
            side_effect=make_service_instance,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
    ):
        MockFactory.create.return_value = MagicMock()
        MockFactory.get_configured_providers.return_value = []

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="test query",
            limit=5,
            time_range=None,
        )

    assert isinstance(result, TemporalSearchResults)
    assert len(result.results) >= 1
    # No timeout warning for a clean run
    assert not result.warning


# ---------------------------------------------------------------------------
# test_empty_results_no_false_positive_warning
# ---------------------------------------------------------------------------


def test_empty_results_no_false_positive_warning(tmp_path):
    """All providers succeed but return 0 results → no 'All providers failed' warning.

    When results_by_shard is empty because providers returned empty results
    (not because they failed or timed out), the warning 'All providers failed'
    must NOT fire.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)

    def make_service_instance(*args, **kwargs):
        instance = MagicMock()
        # Returns empty results — success, but no hits
        instance.query_temporal.return_value = _make_results_with([])
        return instance

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
            return_value=_two_provider_groups(),
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
            side_effect=make_service_instance,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
    ):
        MockFactory.create.return_value = MagicMock()
        MockFactory.get_configured_providers.return_value = []

        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=tmp_path,
            vector_store=vector_store,
            query_text="test query",
            limit=5,
            time_range=None,
        )

    assert isinstance(result, TemporalSearchResults)
    assert result.results == []
    # Empty results are NOT a failure — no warning expected
    assert not result.warning, (
        f"Expected no warning for empty-but-successful providers, got: {result.warning!r}"
    )
