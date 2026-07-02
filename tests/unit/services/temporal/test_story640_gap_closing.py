"""Tests for Story #640 gap closures — dual-provider temporal implementation.

Covers:
1. _create_embedding_provider_for_collection: correct provider per collection name
2. sanitize_model_name: public function exported from temporal_collection_naming
3. TemporalIndexer thread count: provider-aware (cohere vs voyage-ai)
4. filter_healthy_temporal_providers wired into fusion dispatch
5. record_temporal_success/failure wired into fusion dispatch query path
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
    sanitize_model_name,
    resolve_temporal_collection_name,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _create_embedding_provider_for_collection,
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)
from code_indexer.services.provider_health_monitor import (
    DEFAULT_DOWN_CONSECUTIVE_FAILURES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_voyage_config():
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai.model = "voyage-code-3"
    config.cohere.model = "embed-v4.0"
    return config


def _make_cohere_config():
    config = MagicMock()
    config.embedding_provider = "cohere"
    config.voyage_ai.model = "voyage-code-3"
    config.cohere.model = "embed-v4.0"
    return config


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


def _fake_subprocess_run_one_commit(cmd, **kwargs):
    """Return one fake commit for git log, and branch name for git branch calls."""
    import subprocess

    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    if "branch" in cmd:
        mock_result.stdout = "main\n"
    elif "log" in cmd:
        mock_result.stdout = (
            "abc1234567890123456789012345678901234567890"
            "\x001699999999\x00Test Author\x00test@example.com"
            "\x00Test commit message\x00\x1e"
        )
    else:
        mock_result.stdout = ""
    mock_result.returncode = 0
    return mock_result


def _assert_success_recorded(
    monitor, health_key, index_path, mock_vector_store, config
):
    """Run a successful fusion query and assert success was recorded in ProviderHealthMonitor."""
    mock_results = _make_results_with([_make_result()])
    with (
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
        ) as mock_svc_cls,
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
    ):
        mock_factory.create.return_value = MagicMock()
        mock_svc = MagicMock()
        mock_svc.query_temporal.return_value = mock_results
        mock_svc_cls.return_value = mock_svc
        execute_temporal_query_with_fusion(
            config=config,
            index_path=index_path,
            vector_store=mock_vector_store,
            query_text="search",
            limit=5,
        )
    status = monitor.get_health(health_key).get(health_key)
    assert status is not None, (
        "record_temporal_success NOT called: no health data after successful query"
    )
    assert status.status != "down", (
        f"Provider unexpectedly 'down' after success: {status}"
    )


def _assert_failure_recorded(
    monitor, health_key, index_path, mock_vector_store, config
):
    """Run a failing fusion query and assert failure was recorded in ProviderHealthMonitor."""
    with (
        patch(
            "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
        ) as mock_svc_cls,
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
    ):
        mock_factory.create.return_value = MagicMock()
        mock_svc = MagicMock()
        mock_svc.query_temporal.side_effect = RuntimeError("embedding unavailable")
        mock_svc_cls.return_value = mock_svc
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=mock_vector_store,
                query_text="search",
                limit=5,
            )
    status = monitor.get_health(health_key).get(health_key)
    assert status is not None, (
        "record_temporal_failure NOT called: no health data after failed query"
    )
    consecutive = monitor._consecutive_failures.get(health_key, 0)
    assert consecutive >= 1, (
        f"Expected >=1 consecutive failure recorded, got {consecutive}"
    )


def _run_indexer_and_capture_thread_count(
    provider: str,
    cohere_parallel: int,
    voyage_parallel: int,
    collection_suffix: str,
    tmp_path,
) -> int:
    """Run TemporalIndexer.index_commits() and return the thread count passed to VCM."""
    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

    mock_config = MagicMock()
    mock_config.embedding_provider = provider
    mock_config.cohere.parallel_requests = cohere_parallel
    mock_config.voyage_ai.parallel_requests = voyage_parallel
    mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
    mock_config.voyage_ai.model = "voyage-code-3"
    mock_config.cohere.model = "embed-v4.0"
    mock_config.temporal.diff_context_lines = 3
    mock_config.file_extensions = []
    mock_config.override_config = None

    mock_config_manager = MagicMock()
    mock_config_manager.get_config.return_value = mock_config
    mock_config_manager.config_path = tmp_path / ".code-indexer" / "config.json"

    mock_vector_store = MagicMock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
    mock_vector_store.collection_exists.return_value = True
    mock_vector_store.load_id_index.return_value = set()

    indexer = TemporalIndexer(
        mock_config_manager,
        mock_vector_store,
        collection_name=f"code-indexer-temporal-{collection_suffix}",
    )

    captured: list = []

    def fake_vcm(embedding_provider, thread_count, **kwargs):
        captured.append(thread_count)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with (
        patch(
            "code_indexer.services.temporal.temporal_indexer.VectorCalculationManager",
            side_effect=fake_vcm,
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
        patch(
            "code_indexer.services.temporal.temporal_indexer.subprocess.run",
            side_effect=_fake_subprocess_run_one_commit,
        ),
    ):
        mock_factory.create.return_value = MagicMock()
        indexer.index_commits()

    return captured[0] if captured else -1


# ---------------------------------------------------------------------------
# test_sanitize_model_name_public_function
# ---------------------------------------------------------------------------


def test_sanitize_model_name_public_function():
    """sanitize_model_name must be publicly importable and produce correct slugs."""
    assert sanitize_model_name("voyage-code-3") == "voyage_code_3"
    assert sanitize_model_name("embed-v4.0") == "embed_v4_0"
    model = "voyage-code-3"
    slug = sanitize_model_name(model)
    assert (
        resolve_temporal_collection_name(model) == f"{TEMPORAL_COLLECTION_PREFIX}{slug}"
    )


# ---------------------------------------------------------------------------
# test_create_embedding_provider_for_collection_*
# ---------------------------------------------------------------------------


def test_create_embedding_provider_for_collection_voyage():
    """Voyage collection name must resolve to the VoyageAI embedding provider."""
    config = _make_voyage_config()
    collection_name = (
        f"{TEMPORAL_COLLECTION_PREFIX}{sanitize_model_name('voyage-code-3')}"
    )
    mock_voyage_provider = MagicMock(name="VoyageProvider")

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.get_model_name_for_provider"
        ) as mock_get_model,
    ):
        mock_factory.get_configured_providers.return_value = ["voyage-ai", "cohere"]
        mock_get_model.side_effect = lambda p, cfg: (
            "voyage-code-3" if p == "voyage-ai" else "embed-v4.0"
        )
        mock_factory.create.return_value = mock_voyage_provider
        provider = _create_embedding_provider_for_collection(config, collection_name)

    mock_factory.create.assert_called_once_with(config, provider_name="voyage-ai")
    assert provider is mock_voyage_provider


def test_create_embedding_provider_for_collection_cohere():
    """Cohere collection name must resolve to the Cohere embedding provider."""
    config = _make_cohere_config()
    collection_name = f"{TEMPORAL_COLLECTION_PREFIX}{sanitize_model_name('embed-v4.0')}"
    mock_cohere_provider = MagicMock(name="CohereProvider")

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.get_model_name_for_provider"
        ) as mock_get_model,
    ):
        mock_factory.get_configured_providers.return_value = ["voyage-ai", "cohere"]
        mock_get_model.side_effect = lambda p, cfg: (
            "voyage-code-3" if p == "voyage-ai" else "embed-v4.0"
        )
        mock_factory.create.return_value = mock_cohere_provider
        provider = _create_embedding_provider_for_collection(config, collection_name)

    mock_factory.create.assert_called_once_with(config, provider_name="cohere")
    assert provider is mock_cohere_provider


def test_create_embedding_provider_for_collection_legacy_fallback():
    """Legacy collection name ('code-indexer-temporal') must fall back to primary provider."""
    config = _make_voyage_config()
    collection_name = "code-indexer-temporal"  # legacy: no model slug
    mock_provider = MagicMock(name="FallbackProvider")

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
        ) as mock_factory,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.get_model_name_for_provider"
        ) as mock_get_model,
    ):
        mock_factory.get_configured_providers.return_value = ["voyage-ai"]
        mock_get_model.return_value = "voyage-code-3"
        mock_factory.create.return_value = mock_provider
        provider = _create_embedding_provider_for_collection(config, collection_name)

    mock_factory.create.assert_called_once_with(config)
    assert provider is mock_provider


# ---------------------------------------------------------------------------
# test_temporal_indexer_thread_count_cohere / _voyage
# ---------------------------------------------------------------------------


def test_temporal_indexer_thread_count_cohere(tmp_path):
    """When embedding_provider is 'cohere', TemporalIndexer uses cohere.parallel_requests."""
    thread_count = _run_indexer_and_capture_thread_count(
        provider="cohere",
        cohere_parallel=6,
        voyage_parallel=3,
        collection_suffix="embed_v4_0",
        tmp_path=tmp_path,
    )
    assert thread_count == 6, (
        f"Expected cohere.parallel_requests=6 as thread count, got {thread_count}"
    )


def test_temporal_indexer_thread_count_voyage(tmp_path):
    """When embedding_provider is 'voyage-ai', TemporalIndexer uses voyage_ai.parallel_requests."""
    thread_count = _run_indexer_and_capture_thread_count(
        provider="voyage-ai",
        cohere_parallel=6,
        voyage_parallel=3,
        collection_suffix="voyage_code_3",
        tmp_path=tmp_path,
    )
    assert thread_count == 3, (
        f"Expected voyage_ai.parallel_requests=3 as thread count, got {thread_count}"
    )


# ---------------------------------------------------------------------------
# test_filter_healthy_wired_in_dispatch
# ---------------------------------------------------------------------------


def test_filter_healthy_wired_in_dispatch(tmp_path):
    """filter_healthy_temporal_providers must gate queries in execute_temporal_query_with_fusion.

    Uses the real ProviderHealthMonitor to trip the circuit breaker for one collection.
    Verifies only the healthy collection is queried and the unhealthy one is skipped.
    """
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
    from code_indexer.services.temporal.temporal_health import make_temporal_health_key

    ProviderHealthMonitor.reset_instance()
    try:
        config = _make_voyage_config()
        index_path = tmp_path / "index"
        index_path.mkdir()

        healthy_collection = "code-indexer-temporal-voyage_code_3"
        unhealthy_collection = "code-indexer-temporal-embed_v4_0"
        (index_path / healthy_collection).mkdir()
        (index_path / unhealthy_collection).mkdir()

        monitor = ProviderHealthMonitor.get_instance()
        health_key = make_temporal_health_key(unhealthy_collection)
        for _ in range(DEFAULT_DOWN_CONSECUTIVE_FAILURES):
            monitor.record_call(health_key, latency_ms=100.0, success=False)

        mock_vector_store = MagicMock()
        mock_vector_store.project_root = tmp_path
        mock_results = _make_results_with([_make_result()])
        collections_queried: list = []

        def capture_collection(*, collection_name, **kwargs):
            collections_queried.append(collection_name)
            svc = MagicMock()
            svc.query_temporal.return_value = mock_results
            return svc

        with (
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService",
                side_effect=capture_collection,
            ),
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
            ) as mock_factory,
        ):
            mock_factory.create.return_value = MagicMock()
            execute_temporal_query_with_fusion(
                config=config,
                index_path=index_path,
                vector_store=mock_vector_store,
                query_text="search",
                limit=5,
            )

        assert collections_queried == [healthy_collection], (
            f"Expected only [{healthy_collection}] to be queried, got {collections_queried}"
        )
    finally:
        ProviderHealthMonitor.reset_instance()


# ---------------------------------------------------------------------------
# test_record_success_failure_wired
# ---------------------------------------------------------------------------


def test_record_success_failure_wired(tmp_path):
    """record_temporal_success/failure must be called by fusion dispatch after queries."""
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
    from code_indexer.services.temporal.temporal_health import make_temporal_health_key

    config = _make_voyage_config()
    index_path = tmp_path / "index"
    index_path.mkdir()
    collection_name = "code-indexer-temporal-voyage_code_3"
    (index_path / collection_name).mkdir()

    mock_vector_store = MagicMock()
    mock_vector_store.project_root = tmp_path
    health_key = make_temporal_health_key(collection_name)

    ProviderHealthMonitor.reset_instance()
    try:
        monitor = ProviderHealthMonitor.get_instance()
        _assert_success_recorded(
            monitor, health_key, index_path, mock_vector_store, config
        )

        ProviderHealthMonitor.reset_instance()
        monitor = ProviderHealthMonitor.get_instance()
        _assert_failure_recorded(
            monitor, health_key, index_path, mock_vector_store, config
        )
    finally:
        ProviderHealthMonitor.reset_instance()
