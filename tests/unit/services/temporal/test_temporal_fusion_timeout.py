"""Tests for Bug #669: TimeoutError handling in _query_multi_provider_fusion.

Verifies that as_completed() TimeoutError is caught at the loop level,
partial results are returned, and futures are cancelled before ThreadPoolExecutor
shutdown to avoid blocking.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _query_multi_provider_fusion,
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


def _make_two_collections(tmp_path):
    """Return a list of two (name, path) collection tuples."""
    return [
        (
            "code-indexer-temporal-voyage_code_3",
            tmp_path / "code-indexer-temporal-voyage_code_3",
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            tmp_path / "code-indexer-temporal-embed_v4_0",
        ),
    ]


def _make_three_collections(tmp_path):
    """Return a list of three (name, path) collection tuples."""
    return [
        (
            "code-indexer-temporal-voyage_code_3",
            tmp_path / "code-indexer-temporal-voyage_code_3",
        ),
        (
            "code-indexer-temporal-embed_v4_0",
            tmp_path / "code-indexer-temporal-embed_v4_0",
        ),
        (
            "code-indexer-temporal-cohere_embed_3",
            tmp_path / "code-indexer-temporal-cohere_embed_3",
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
    - record_temporal_failure is called for each timed-out collection.

    query_provider-level behaviour is driven by TemporalSearchService sleeping
    longer than the forced timeout so as_completed() fires TimeoutError.
    TEMPORAL_QUERY_TIMEOUT_SECONDS is overridden to 0.05s so the test runs fast.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)
    collections = _make_three_collections(tmp_path)

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
        result = _query_multi_provider_fusion(
            config=config,
            vector_store=vector_store,
            collections=collections,
            query_text="test query",
            limit=5,
            time_range=None,
            file_path_filter=None,
        )
        elapsed = time.time() - t0

    assert isinstance(result, TemporalSearchResults)
    assert result.results == []
    assert result.warning is not None
    assert len(result.warning) > 0

    # No-blocking requirement: must return well within timeout + 2s overhead
    assert elapsed < forced_timeout + 2.0, (
        f"_query_multi_provider_fusion blocked for {elapsed:.2f}s "
        f"(timeout={forced_timeout}s, max allowed={forced_timeout + 2.0}s)"
    )

    # Timed-out providers must be recorded as failures in health telemetry
    assert mock_record_failure.called, (
        "record_temporal_failure must be called for timed-out collections"
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
    - record_temporal_failure is called for the two timed-out collections.

    Behavior is keyed on collection name to avoid shared-counter data races
    across the threads spawned by ThreadPoolExecutor.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)
    collections = _make_three_collections(tmp_path)

    fast_coll_name = "code-indexer-temporal-voyage_code_3"
    fast_result = _make_result("fast.py", score=0.95)
    sleep_duration = 0.3
    forced_timeout = 0.1  # fast provider completes at ~0s; slow ones sleep 0.3s

    # Map from collection name → per-instance result behaviour.
    # Each MockService instance is created with a collection_name kwarg so we
    # can key on it deterministically without a shared mutable counter.
    def make_service_instance(*args, **kwargs):
        # TemporalSearchService receives collection_name as a keyword arg
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
        result = _query_multi_provider_fusion(
            config=config,
            vector_store=vector_store,
            collections=collections,
            query_text="test query",
            limit=5,
            time_range=None,
            file_path_filter=None,
        )
        elapsed = time.time() - t0

    assert isinstance(result, TemporalSearchResults)
    # At least the fast result must be present
    assert len(result.results) >= 1
    assert any(r.file_path == "fast.py" for r in result.results)
    # A timeout warning must exist
    assert result.warning is not None
    assert len(result.warning) > 0

    # No-blocking requirement: must return well within timeout + 2s overhead
    assert elapsed < forced_timeout + 2.0, (
        f"_query_multi_provider_fusion blocked for {elapsed:.2f}s "
        f"(timeout={forced_timeout}s, max allowed={forced_timeout + 2.0}s)"
    )

    # Timed-out providers must be recorded as failures in health telemetry
    assert mock_record_failure.called, (
        "record_temporal_failure must be called for timed-out collections"
    )


# ---------------------------------------------------------------------------
# test_no_timeout_returns_all_results
# ---------------------------------------------------------------------------


def test_no_timeout_returns_all_results(tmp_path):
    """Control: all providers complete before timeout → all results, no warning.

    Verifies the happy path is not broken by the timeout-handling fix.
    Behavior is keyed on collection_name to avoid shared-counter data races.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)
    collections = _make_two_collections(tmp_path)

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
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService",
            side_effect=make_service_instance,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as MockFactory,
    ):
        MockFactory.create.return_value = MagicMock()
        MockFactory.get_configured_providers.return_value = []

        result = _query_multi_provider_fusion(
            config=config,
            vector_store=vector_store,
            collections=collections,
            query_text="test query",
            limit=5,
            time_range=None,
            file_path_filter=None,
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

    MEDIUM 2: when results_by_provider is empty because providers returned
    empty results (not because they failed or timed out), the warning
    'All providers failed' must NOT fire.
    """
    config = _make_mock_config()
    vector_store = _make_mock_vector_store(tmp_path)
    collections = _make_two_collections(tmp_path)

    def make_service_instance(*args, **kwargs):
        instance = MagicMock()
        # Returns empty results — success, but no hits
        instance.query_temporal.return_value = _make_results_with([])
        return instance

    with (
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

        result = _query_multi_provider_fusion(
            config=config,
            vector_store=vector_store,
            collections=collections,
            query_text="test query",
            limit=5,
            time_range=None,
            file_path_filter=None,
        )

    assert isinstance(result, TemporalSearchResults)
    assert result.results == []
    # Empty results are NOT a failure — no warning expected
    assert not result.warning, (
        f"Expected no warning for empty-but-successful providers, got: {result.warning!r}"
    )
