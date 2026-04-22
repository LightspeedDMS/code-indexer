"""
Tests for HNSW cache bypass on multi-repo fan-out path.

Bug #881 Phase 3: Multi-repo fan-out calls (via MultiSearchService._search_semantic_sync)
must NOT populate the global HNSW index cache.  Passing hnsw_cache=None to
BackendFactory.create achieves this without any additional cache plumbing.

Single-repo hot-path calls (via SemanticSearchService.search_repository_path default)
must continue to use _server_hnsw_cache to keep cache-hit latency.

These tests capture the hnsw_cache argument passed to BackendFactory.create and verify
the correct value is supplied by each call path.

Named constants throughout — no magic numbers.
"""

import contextlib
import os
import tempfile
from typing import List
from unittest.mock import MagicMock, patch


# Named constants — no magic numbers
FAKE_QUERY = "find authentication logic"
FAKE_LIMIT = 5
FAKE_REPO_ID = "my-repo-global"
MAX_WORKERS = 2
TIMEOUT_SECONDS = 30
MAX_RESULTS_PER_REPO = 10
FAKE_EMBEDDING_DIMENSION = 4
FAKE_EMBEDDING_VALUE = 0.1
FIRST_CAPTURE_INDEX = 0
MIN_BACKEND_CALLS = 1
MOCK_HNSW_CACHE_SENTINEL = (
    object()
)  # stands in for the real _server_hnsw_cache singleton


def _make_captured_backend_factory(captured: List):
    """Return a BackendFactory.create replacement that records the hnsw_cache argument."""

    def factory(config, project_root, hnsw_cache=None, **kwargs):
        captured.append(hnsw_cache)
        mock_vsc = MagicMock()
        mock_vsc.search.return_value = []
        mock_backend = MagicMock()
        mock_backend.get_vector_store_client.return_value = mock_vsc
        return mock_backend

    return factory


def _make_embedding_factory():
    """Return a mock EmbeddingProviderFactory.create result with correct shape."""
    mock_provider = MagicMock()
    mock_provider.embed_query.return_value = [
        FAKE_EMBEDDING_VALUE
    ] * FAKE_EMBEDDING_DIMENSION
    return mock_provider


def _make_config_manager():
    """Return a mock ConfigManager.create_with_backtrack result with minimal config."""
    mock_cfg = MagicMock()
    mock_cfg.embedding_provider = "voyage-ai"
    mock_cfg.collection_name = "code"
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = mock_cfg
    return mock_mgr


@contextlib.contextmanager
def _fake_repo_dir_with_code_indexer():
    """Context manager yielding a temp dir with .code-indexer sub-directory."""
    with tempfile.TemporaryDirectory() as fake_dir:
        os.makedirs(os.path.join(fake_dir, ".code-indexer"), exist_ok=True)
        yield fake_dir


@contextlib.contextmanager
def _shared_search_service_patches(captured: List, fake_dir: str):
    """Patch the shared external dependencies of SemanticSearchService._perform_semantic_search."""
    with (
        patch(
            "code_indexer.server.services.search_service.BackendFactory.create",
            side_effect=_make_captured_backend_factory(captured),
        ),
        patch(
            "code_indexer.server.services.search_service.ConfigManager.create_with_backtrack",
            return_value=_make_config_manager(),
        ),
        patch(
            "code_indexer.server.services.search_service.EmbeddingProviderFactory.create",
            return_value=_make_embedding_factory(),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# Test 1 — fan-out path passes hnsw_cache=None to BackendFactory.create
# ---------------------------------------------------------------------------


def test_fan_out_passes_hnsw_cache_none_to_backend_factory():
    """MultiSearchService._search_semantic_sync must call SemanticSearchService with
    hnsw_cache=None so BackendFactory.create receives None on the fan-out path.

    This prevents fan-out searches from polluting the global HNSW index cache.
    """
    from code_indexer.server.multi.multi_search_service import MultiSearchService
    from code_indexer.server.multi.multi_search_config import MultiSearchConfig
    from code_indexer.server.multi.models import MultiSearchRequest

    captured: List = []

    mock_config = MagicMock(spec=MultiSearchConfig)
    mock_config.max_workers = MAX_WORKERS
    mock_config.timeout_seconds = TIMEOUT_SECONDS
    mock_config.max_results_per_repo = MAX_RESULTS_PER_REPO

    request = MultiSearchRequest(
        repositories=[FAKE_REPO_ID],
        query=FAKE_QUERY,
        search_type="semantic",
        limit=FAKE_LIMIT,
    )

    with _fake_repo_dir_with_code_indexer() as fake_dir:
        with (
            _shared_search_service_patches(captured, fake_dir),
            patch(
                "code_indexer.server.multi.multi_search_service._get_golden_repos_dir",
                return_value=fake_dir,
            ),
            patch(
                "code_indexer.server.app.app.state",
                backend_registry=MagicMock(
                    global_repos=MagicMock(
                        get_repo=MagicMock(return_value={"alias_name": FAKE_REPO_ID})
                    )
                ),
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager.read_alias",
                return_value=fake_dir,
            ),
        ):
            service = MultiSearchService(mock_config)
            service._search_semantic_sync(FAKE_REPO_ID, request)

    assert len(captured) >= MIN_BACKEND_CALLS, (
        "BackendFactory.create was not called — _search_semantic_sync did not reach it"
    )
    assert captured[FIRST_CAPTURE_INDEX] is None, (
        f"Fan-out path must pass hnsw_cache=None to BackendFactory.create, "
        f"got: {captured[FIRST_CAPTURE_INDEX]!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — single-repo hot path uses _server_hnsw_cache (not None)
# ---------------------------------------------------------------------------


def test_single_repo_search_uses_server_hnsw_cache_not_none():
    """SemanticSearchService.search_repository_path (default, non-fan-out path) must call
    BackendFactory.create with _server_hnsw_cache, which is the global singleton
    and is NOT None.

    This verifies the single-repo hot path retains its cache-hit latency.
    """
    from code_indexer.server.services.search_service import SemanticSearchService
    from code_indexer.server.models.api_models import SemanticSearchRequest

    captured: List = []

    search_request = SemanticSearchRequest(
        query=FAKE_QUERY,
        limit=FAKE_LIMIT,
    )

    with _fake_repo_dir_with_code_indexer() as fake_dir:
        with (
            _shared_search_service_patches(captured, fake_dir),
            patch(
                "code_indexer.server.app._server_hnsw_cache",
                MOCK_HNSW_CACHE_SENTINEL,
            ),
        ):
            svc = SemanticSearchService()
            svc.search_repository_path(fake_dir, search_request)

    assert len(captured) >= MIN_BACKEND_CALLS, (
        "BackendFactory.create was not called — search_repository_path did not reach it"
    )
    assert captured[FIRST_CAPTURE_INDEX] is not None, (
        "Single-repo hot path must use _server_hnsw_cache (not None)"
    )
    assert captured[FIRST_CAPTURE_INDEX] is MOCK_HNSW_CACHE_SENTINEL, (
        f"Single-repo hot path must pass _server_hnsw_cache singleton to BackendFactory.create, "
        f"got: {captured[FIRST_CAPTURE_INDEX]!r}"
    )
