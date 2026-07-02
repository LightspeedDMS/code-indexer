"""Unit tests for temporal routing gates in SemanticQueryManager.

Bug fix: has_temporal_params at lines ~590 and ~1380 must include chunk_type,
diff_type, and author in the routing condition. Without the fix, queries with
only chunk_type/diff_type/author skip the temporal index entirely.

- Gate 1: _search_single_repository routing (~line 1380) — verified by observing
  that execute_temporal_query_with_fusion (external function) is called.
- Gate 2: query_user_repositories warning (~line 590) — verified by observing
  that the "warning" key appears in the response when temporal params yield
  empty results.

Tests must FAIL before the fix and PASS after the fix.

Implementation note: _both_providers_configured is patched to return False in
Gate 1 tests so query_strategy resolves to "primary_only" (the path that reaches
the temporal routing check at line ~1380). The parallel/failover strategy paths
are separate code branches that are not affected by this bug fix.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from code_indexer.server.query.semantic_query_manager import SemanticQueryManager
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def activated_repo_manager_mock():
    mock = MagicMock()
    mock.list_activated_repositories.return_value = [
        {
            "user_alias": "my-repo",
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2024-01-01T00:00:00Z",
            "last_accessed": "2024-01-01T00:00:00Z",
        },
    ]

    def get_repo_path(username, user_alias):
        p = Path(tempfile.gettempdir()) / f"repos-{username}-{user_alias}"
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    mock.get_activated_repo_path.side_effect = get_repo_path
    return mock


@pytest.fixture
def manager(temp_data_dir, activated_repo_manager_mock):
    return SemanticQueryManager(
        data_dir=temp_data_dir,
        activated_repo_manager=activated_repo_manager_mock,
    )


def _no_index_temporal_results():
    """Return TemporalSearchResults indicating no temporal index available."""
    return TemporalSearchResults(
        results=[],
        query="test",
        filter_type="temporal",
        filter_value=None,
        warning="Temporal index not available.",
    )


def _temporal_infrastructure_patches():
    """Return patches for all external collaborators in _execute_temporal_query.

    Returns a tuple:
        (cm_patch, bf_patch, app_cache_patch, sm_cache_patch, fusion_patch)

    Both cache patches are needed because the import inside _execute_temporal_query
    is lazy (from ..app import _server_hnsw_cache), so we patch both the source
    and any possible bound reference.
    """
    mock_config = MagicMock()
    mock_config.embedding_provider = "voyage-ai"
    mock_config.get.return_value = None

    mock_vector_store = MagicMock()
    mock_backend = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vector_store

    cm_patch = patch(
        "code_indexer.proxy.config_manager.ConfigManager.create_with_backtrack",
        return_value=MagicMock(get_config=MagicMock(return_value=mock_config)),
    )
    bf_patch = patch(
        "code_indexer.backends.backend_factory.BackendFactory.create",
        return_value=mock_backend,
    )
    app_cache_patch = patch(
        "code_indexer.server.app._server_hnsw_cache",
        MagicMock(),
    )
    sm_cache_patch = patch(
        "code_indexer.server.query.semantic_query_manager._server_hnsw_cache",
        MagicMock(),
        create=True,
    )
    fusion_patch = patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch"
        ".execute_temporal_query_with_fusion",
        return_value=_no_index_temporal_results(),
    )
    return cm_patch, bf_patch, app_cache_patch, sm_cache_patch, fusion_patch


# ---------------------------------------------------------------------------
# Gate 1: _search_single_repository routing (line ~1380)
# Observable: execute_temporal_query_with_fusion is called when routing fires.
# _both_providers_configured is patched to False so query_strategy stays as
# "primary_only" — the path that reaches the temporal routing check.
# ---------------------------------------------------------------------------


class TestGate1SearchSingleRepositoryRouting:
    """Verify chunk_type/diff_type/author route to temporal execution path."""

    @pytest.mark.parametrize(
        "extra_kwargs",
        [
            {"chunk_type": "commit_diff"},
            {"diff_type": "added"},
            {"author": "Alice"},
        ],
    )
    def test_temporal_param_alone_calls_temporal_fusion(
        self, manager, extra_kwargs, tmp_path
    ):
        """chunk_type / diff_type / author alone must invoke execute_temporal_query_with_fusion."""
        repo_path = str(tmp_path / "my-repo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        cm_patch, bf_patch, app_cache_patch, sm_cache_patch, fusion_patch = (
            _temporal_infrastructure_patches()
        )

        # Force primary_only path so execution reaches the temporal routing check
        single_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=False
        )

        with (
            cm_patch,
            bf_patch,
            app_cache_patch,
            sm_cache_patch,
            single_provider_patch,
            fusion_patch as mock_fusion,
        ):
            manager._search_single_repository(
                repo_path=repo_path,
                repository_alias="my-repo",
                query_text="test query",
                limit=10,
                min_score=None,
                file_extensions=None,
                time_range=None,
                time_range_all=False,
                at_commit=None,
                show_evolution=False,
                **extra_kwargs,
            )

        mock_fusion.assert_called_once()

    @pytest.mark.parametrize(
        "extra_kwargs",
        [
            {"chunk_type": None},
            {"diff_type": None},
            {"author": None},
        ],
    )
    def test_none_temporal_param_does_not_call_temporal_fusion(
        self, manager, extra_kwargs, tmp_path
    ):
        """None chunk_type / diff_type / author must NOT invoke execute_temporal_query_with_fusion."""
        repo_path = str(tmp_path / "my-repo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        _, _, _, _, fusion_patch = _temporal_infrastructure_patches()

        single_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=False
        )

        with (
            fusion_patch as mock_fusion,
            single_provider_patch,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as mock_ss,
        ):
            mock_ss_instance = MagicMock()
            mock_ss_instance.search.return_value = []
            mock_ss.return_value = mock_ss_instance

            manager._search_single_repository(
                repo_path=repo_path,
                repository_alias="my-repo",
                query_text="test query",
                limit=10,
                min_score=None,
                file_extensions=None,
                time_range=None,
                time_range_all=False,
                at_commit=None,
                show_evolution=False,
                **extra_kwargs,
            )

        mock_fusion.assert_not_called()


# ---------------------------------------------------------------------------
# Gate 2: query_user_repositories warning message (line ~590)
# Observable: "warning" key present in response when temporal params yield no results
# ---------------------------------------------------------------------------


class TestGate2WarningMessagePresence:
    """Verify chunk_type/diff_type/author produce the temporal-index warning."""

    @pytest.mark.parametrize(
        "extra_kwargs",
        [
            {"chunk_type": "commit_diff"},
            {"diff_type": "added"},
            {"author": "Alice"},
        ],
    )
    def test_temporal_param_alone_produces_warning_on_empty_results(
        self, manager, extra_kwargs
    ):
        """When chunk_type/diff_type/author given but no temporal index exists,
        response must include the temporal-index warning message.
        """
        cm_patch, bf_patch, app_cache_patch, sm_cache_patch, fusion_patch = (
            _temporal_infrastructure_patches()
        )

        single_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=False
        )

        with (
            cm_patch,
            bf_patch,
            app_cache_patch,
            sm_cache_patch,
            single_provider_patch,
            fusion_patch,
        ):
            result = manager.query_user_repositories(
                username="testuser",
                query_text="test query",
                **extra_kwargs,
            )

        assert "warning" in result, (
            f"Expected 'warning' key in response for {extra_kwargs}. "
            f"Got keys: {list(result.keys())}"
        )

    @pytest.mark.parametrize(
        "extra_kwargs",
        [
            {"chunk_type": None},
            {"diff_type": None},
            {"author": None},
        ],
    )
    def test_none_temporal_params_do_not_produce_warning(self, manager, extra_kwargs):
        """None values must not produce the temporal warning."""
        single_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=False
        )

        with (
            single_provider_patch,
            patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as mock_ss,
        ):
            mock_ss_instance = MagicMock()
            mock_ss_instance.search.return_value = []
            mock_ss.return_value = mock_ss_instance

            result = manager.query_user_repositories(
                username="testuser",
                query_text="test query",
                **extra_kwargs,
            )

        assert "warning" not in result


# ---------------------------------------------------------------------------
# Gate 3: Dual-provider bypass regression (Bug #667)
# When both providers are configured, query_strategy auto-sets to "parallel"
# which returned early (line ~1354) BEFORE the temporal routing gate (line ~1387).
# Fix: skip parallel strategy when temporal params are present.
# Observable: execute_temporal_query_with_fusion called (temporal) vs NOT called
#             (parallel semantic).
# ---------------------------------------------------------------------------


class TestGate3TemporalRoutingWithDualProviders:
    """Bug #667: verify temporal routing fires even with dual providers configured."""

    @pytest.mark.parametrize(
        "extra_kwargs",
        [
            {"chunk_type": "commit_diff"},
            {"diff_type": "added"},
            {"author": "Alice"},
        ],
    )
    def test_temporal_param_with_dual_providers_calls_temporal_fusion(
        self, manager, extra_kwargs, tmp_path
    ):
        """chunk_type/diff_type/author must invoke temporal fusion even when both
        providers are configured (dual-provider should NOT override temporal routing).

        Before Bug #667 fix: query_strategy was set to "parallel", parallel block
        ran and returned early — temporal fusion was never called.
        After fix: temporal params prevent parallel strategy, temporal gate fires.
        """
        repo_path = str(tmp_path / "my-repo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        cm_patch, bf_patch, app_cache_patch, sm_cache_patch, fusion_patch = (
            _temporal_infrastructure_patches()
        )

        # Simulate dual-provider configuration — this is the Bug #667 trigger
        dual_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=True
        )

        with (
            cm_patch,
            bf_patch,
            app_cache_patch,
            sm_cache_patch,
            dual_provider_patch,
            fusion_patch as mock_fusion,
        ):
            manager._search_single_repository(
                repo_path=repo_path,
                repository_alias="my-repo",
                query_text="test query",
                limit=10,
                min_score=None,
                file_extensions=None,
                time_range=None,
                time_range_all=False,
                at_commit=None,
                show_evolution=False,
                **extra_kwargs,
            )

        (
            mock_fusion.assert_called_once(),
            (
                f"Bug #667 regression: temporal fusion not called with dual providers "
                f"and {extra_kwargs}. Parallel strategy bypassed temporal routing gate."
            ),
        )

    def test_no_temporal_params_with_dual_providers_does_not_call_temporal_fusion(
        self, manager, tmp_path
    ):
        """Without temporal params, dual-provider should use parallel semantic search
        (not temporal). Control test to verify the fix doesn't break the non-temporal path.
        """
        repo_path = str(tmp_path / "my-repo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        _, _, _, _, fusion_patch = _temporal_infrastructure_patches()

        dual_provider_patch = patch.object(
            manager, "_both_providers_configured", return_value=True
        )

        with (
            fusion_patch as mock_fusion,
            dual_provider_patch,
            patch.object(manager, "_search_with_provider", return_value=[]),
        ):
            manager._search_single_repository(
                repo_path=repo_path,
                repository_alias="my-repo",
                query_text="test query",
                limit=10,
                min_score=None,
                file_extensions=None,
                time_range=None,
                time_range_all=False,
                at_commit=None,
                show_evolution=False,
                # No temporal params
            )

        (
            mock_fusion.assert_not_called(),
            (
                "Without temporal params, dual-provider parallel path should run, "
                "not temporal fusion."
            ),
        )
