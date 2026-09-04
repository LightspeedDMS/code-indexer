"""Bug #1760 (Fix 2/2) -- a total provider failure in parallel dispatch must
surface as a real error, never a silent success:true/total_results:0.

Root cause: `_search_single_repository`'s "parallel" query_strategy branch
(both voyage-ai and cohere configured) dispatches both providers concurrently
via `ThreadPoolExecutor`. When `future.result()` raises, the handler logs a
WARNING and (for anything other than `LocalIndexNotFoundError`) records a
provider-health failure -- but it NEVER checks whether every dispatched
provider ended up failing. `primary_results`/`secondary_results` simply stay
at their initialized `[]`, fusion produces `[]`, and the function returns
`[]` -- indistinguishable from a genuine zero-match query. This propagates
all the way up through `_perform_search` (which treats an empty return with
no raised exception as a normal, successful zero-result repo) to
`query_user_repositories`, and the MCP/REST caller receives
`{"success": true, "total_results": 0}` for what was actually a hard
storage/provider failure (e.g. the confirmed live incident: SQLite
"attempt to write a readonly database" on BOTH providers' local index-read
attempt).

Fix contract (tested here):
1. When EVERY dispatched provider raises a HARD failure (anything other
   than LocalIndexNotFoundError, which Bug #1236 established as a
   legitimate "no index yet" signal) and none produced any results, the
   parallel-dispatch branch must raise -- never silently return [].
2. A PARTIAL failure (one provider hard-fails, the other succeeds -- even
   with a genuinely empty result) must NOT raise -- existing graceful
   degradation is preserved.
3. Both providers genuinely succeeding with zero matches (no exception at
   all) must NOT raise -- a real "no matches" result stays silent.
4. Both providers raising LocalIndexNotFoundError (no index built yet) must
   NOT raise -- preserves Bug #1236's existing "index not present yet"
   contract.
"""

import logging
import shutil
import tempfile
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    SemanticQueryError,
    QueryResult,
)
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor


def _make_provider_results(
    provider: str, file_path: str, score: float
) -> List[QueryResult]:
    return [
        QueryResult(
            file_path=file_path,
            line_number=1,
            code_snippet=f"code from {provider}",
            similarity_score=score,
            repository_alias="test-repo",
            source_provider=provider,
        )
    ]


def _fast_search(*args, **kwargs) -> List[QueryResult]:
    provider = kwargs.get("provider_name", "unknown")
    return _make_provider_results(provider, f"src/{provider}.py", 0.75)


def _patch_health_monitor(monitor: ProviderHealthMonitor):
    return patch(
        "code_indexer.server.query.semantic_query_manager.ProviderHealthMonitor",
        get_instance=MagicMock(return_value=monitor),
    )


def _run_parallel_query(manager, repo_path):
    return manager._search_single_repository(
        repo_path=repo_path,
        repository_alias="test-repo",
        query_text="authentication",
        limit=10,
        min_score=None,
        file_extensions=None,
        query_strategy="parallel",
    )


@pytest.fixture
def repo_path():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_health_monitor():
    ProviderHealthMonitor.reset_instance()
    yield
    ProviderHealthMonitor.reset_instance()


@pytest.fixture
def health_monitor():
    return ProviderHealthMonitor.get_instance()


@pytest.fixture
def manager():
    m = SemanticQueryManager.__new__(SemanticQueryManager)
    m.data_dir = "/fake/data"
    m.query_timeout_seconds = 30
    m.max_concurrent_queries_per_user = 5
    m.max_results_per_query = 100
    m._active_queries_per_user = {}
    m.logger = logging.getLogger(__name__)
    mock_arm = MagicMock()
    mock_arm.activated_repos_dir = "/fake/data/activated_repos"
    m.activated_repo_manager = mock_arm
    m.background_job_manager = MagicMock()
    return m


class TestTotalProviderFailureSurfacesRealError:
    """The discriminating fix: when EVERY dispatched provider hard-fails,
    the caller must see a real exception, never a silent empty success."""

    def test_both_providers_readonly_db_error_raises(
        self, manager, repo_path, health_monitor
    ):
        """The exact confirmed production scenario: both voyage-ai and
        cohere raise sqlite3.OperationalError('attempt to write a readonly
        database') on the local chunk-store read. Must raise, not return []."""
        import sqlite3

        def raising_both(*args, **kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        with patch.object(manager, "_search_with_provider", side_effect=raising_both):
            with _patch_health_monitor(health_monitor):
                with pytest.raises(Exception) as exc_info:
                    _run_parallel_query(manager, repo_path)

        assert "readonly database" in str(exc_info.value)

    def test_both_providers_generic_runtime_error_raises(
        self, manager, repo_path, health_monitor
    ):
        """Any hard failure type (not just sqlite) must raise when total."""

        def raising_both(*args, **kwargs):
            raise RuntimeError("storage backend unavailable")

        with patch.object(manager, "_search_with_provider", side_effect=raising_both):
            with _patch_health_monitor(health_monitor):
                with pytest.raises(Exception):
                    _run_parallel_query(manager, repo_path)


class TestPartialFailureStaysGraceful:
    """One provider hard-failing while the other succeeds must NOT raise --
    existing graceful degradation (serve what succeeded) is preserved."""

    def test_one_hard_failure_one_success_does_not_raise(
        self, manager, repo_path, health_monitor
    ):
        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise RuntimeError("voyage-ai unavailable")
            return _fast_search(*args, **kwargs)

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                results = _run_parallel_query(manager, repo_path)

        assert len(results) >= 1
        assert any(r.source_provider == "cohere" for r in results)

    def test_one_hard_failure_one_genuinely_empty_success_does_not_raise(
        self, manager, repo_path, health_monitor
    ):
        """Even when the SURVIVING provider's successful result is itself
        an empty list (genuinely zero matches from that provider), a
        partial failure must still NOT raise -- only a TOTAL failure
        (every dispatched provider hard-failing) should. An implementation
        that mistakenly treats "no results" as equivalent to "no provider
        succeeded" would incorrectly raise here."""

        def raising_voyage(*args, **kwargs):
            if kwargs.get("provider_name") == "voyage-ai":
                raise RuntimeError("voyage-ai unavailable")
            return []

        with patch.object(manager, "_search_with_provider", side_effect=raising_voyage):
            with _patch_health_monitor(health_monitor):
                results = _run_parallel_query(manager, repo_path)

        assert results == []


class TestGenuineZeroMatchesStaysSilent:
    """Both providers genuinely succeed with zero results -- a real 'no
    matches' outcome must NOT be turned into an error."""

    def test_both_providers_succeed_with_empty_results_does_not_raise(
        self, manager, repo_path, health_monitor
    ):
        def empty_search(*args, **kwargs):
            return []

        with patch.object(manager, "_search_with_provider", side_effect=empty_search):
            with _patch_health_monitor(health_monitor):
                results = _run_parallel_query(manager, repo_path)

        assert results == []


class TestLocalIndexNotFoundStaysSilent:
    """Both providers raising LocalIndexNotFoundError (Bug #1236's
    legitimate 'no index built yet' signal) must NOT raise -- this is a
    real, non-error empty-result state, not a hard failure."""

    def test_both_providers_local_index_not_found_does_not_raise(
        self, manager, repo_path, health_monitor
    ):
        from code_indexer.storage.filesystem_vector_store import (
            LocalIndexNotFoundError,
        )

        def raising_both(*args, **kwargs):
            raise LocalIndexNotFoundError(
                "HNSW index not found for collection 'main'. "
                "Run: cidx index --rebuild-index"
            )

        with patch.object(manager, "_search_with_provider", side_effect=raising_both):
            with _patch_health_monitor(health_monitor):
                results = _run_parallel_query(manager, repo_path)

        assert results == []


class TestAllProvidersPreSkippedSinbinnedSurfacesRealError:
    """Finding 2: when EVERY provider is pre-skipped as sin-binned before
    dispatch, provider_tasks/futures end up empty, so the existing
    all-dispatched-hard-failed guard never fires -- must raise
    SemanticQueryError instead of silently falling through to [].

    `_search_with_provider` is mocked (matching this file's established
    pattern in TestTotalProviderFailureSurfacesRealError above) because
    the unit under test is the DISPATCH-LEVEL routing/guard logic in
    `_search_single_repository`'s parallel branch, not the provider
    search implementation itself; the assertion that it is never called
    is itself part of the behavior this test proves."""

    def test_both_providers_sinbinned_via_real_health_monitor_raises(
        self, manager, repo_path, health_monitor
    ):
        # Real sin-bin marking mechanism -- the same one the pre-skip
        # check reads via is_sinbinned() -- not a hand-rolled substitute.
        health_monitor.sinbin("voyage-ai")
        health_monitor.sinbin("cohere")

        with patch.object(manager, "_search_with_provider") as mock_search:
            with _patch_health_monitor(health_monitor):
                with pytest.raises(SemanticQueryError) as exc_info:
                    _run_parallel_query(manager, repo_path)

        mock_search.assert_not_called()
        assert type(exc_info.value) is SemanticQueryError
        assert "sinbin" in str(exc_info.value).lower()


class TestPartialPreSkipStaysGraceful:
    """Only ONE provider pre-skipped as sin-binned (the other healthy and
    succeeding) must NOT raise -- the guard is scoped to TOTAL pre-skip
    only."""

    def test_one_provider_sinbinned_other_succeeds_does_not_raise(
        self, manager, repo_path, health_monitor
    ):
        health_monitor.sinbin("voyage-ai")

        with patch.object(manager, "_search_with_provider", side_effect=_fast_search):
            with _patch_health_monitor(health_monitor):
                results = _run_parallel_query(manager, repo_path)

        assert len(results) >= 1
        assert any(r.source_provider == "cohere" for r in results)
        assert all(r.source_provider != "voyage-ai" for r in results)


class TestZeroProvidersConfiguredPathUnchanged:
    """When `_both_providers_configured()` is False, auto-strategy
    resolution picks 'primary_only' -- the parallel-dispatch block (and
    the new pre-skip-sinbinned guard) is never entered. Downstream
    SemanticSearchService is mocked so this stays an isolated unit test
    of the routing decision, not an integration test of real server
    infrastructure. `_both_providers_configured` is mocked (an internal
    method) because it is the exact routing input this test needs to
    control deterministically -- driving it via real API-key config would
    make the test depend on environment/global config state instead of
    the routing logic itself."""

    def test_zero_providers_configured_never_enters_parallel_branch(
        self, manager, repo_path, health_monitor
    ):
        _effective_strategy: list = []
        mock_response = MagicMock()
        mock_response.results = []

        with patch.object(manager, "_both_providers_configured", return_value=False):
            with patch(
                "code_indexer.server.services.search_service.SemanticSearchService"
            ) as mock_service_cls:
                mock_service_cls.return_value.search_repository_path.return_value = (
                    mock_response
                )
                with _patch_health_monitor(health_monitor):
                    results = manager._search_single_repository(
                        repo_path=repo_path,
                        repository_alias="test-repo",
                        query_text="authentication",
                        limit=10,
                        min_score=None,
                        file_extensions=None,
                        query_strategy=None,
                        _effective_strategy_out=_effective_strategy,
                    )

        assert _effective_strategy == ["primary_only"]
        assert results == []
        mock_service_cls.return_value.search_repository_path.assert_called_once()
