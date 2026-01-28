"""
TDD tests for Story #51: MultiSearchService Sync Conversion.

Verifies AC1: MultiSearchService converted from async def to def
while maintaining internal ThreadPoolExecutor for parallel multi-repo search.

Written following TDD methodology - tests first, implementation second.
"""

import importlib
import pytest
from unittest.mock import Mock, patch
from code_indexer.server.multi.multi_search_config import MultiSearchConfig
from code_indexer.server.multi.models import (
    MultiSearchRequest,
    MultiSearchResponse,
)


@pytest.fixture(autouse=True)
def reload_multi_search_service():
    """Reload the multi_search_service module before each test to ensure clean state.

    This prevents mock leakage from concurrent execution tests in other files.
    """
    import code_indexer.server.multi.multi_search_service as mss
    importlib.reload(mss)
    yield


class TestMultiSearchServiceSyncInterface:
    """Test that MultiSearchService has sync interface (AC1)."""

    def test_search_is_synchronous_method(self):
        """MultiSearchService.search() should be a synchronous method, not async.

        AC1: Convert MultiSearchService public methods from async def to def
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        import inspect

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        # Verify search is NOT a coroutine function (async)
        assert not inspect.iscoroutinefunction(
            service.search
        ), "search() should be sync, not async"

    def test_search_returns_response_directly(self):
        """MultiSearchService.search() should return response directly without await.

        AC1: Remove async/await from method signatures and internal calls
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="test",
            search_type="semantic",
            limit=10,
        )

        # Mock the internal search method to avoid actual search
        with patch.object(
            service, "_search_single_repo_sync", return_value=[]
        ):
            # Should be callable synchronously without await
            response = service.search(request)

            # Should return MultiSearchResponse directly
            assert isinstance(response, MultiSearchResponse)

    def test_search_threaded_is_synchronous(self):
        """MultiSearchService._search_threaded() should be synchronous.

        AC1: Convert internal methods from async to sync
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        import inspect

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        assert not inspect.iscoroutinefunction(
            service._search_threaded
        ), "_search_threaded() should be sync"

    def test_search_regex_subprocess_is_synchronous(self):
        """MultiSearchService._search_regex_subprocess() should be synchronous.

        AC1: Convert internal methods from async to sync
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        import inspect

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        assert not inspect.iscoroutinefunction(
            service._search_regex_subprocess
        ), "_search_regex_subprocess() should be sync"

    def test_execute_parallel_search_is_synchronous(self):
        """MultiSearchService._execute_parallel_search() should be synchronous.

        AC1: Convert internal methods from async to sync
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        import inspect

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        assert not inspect.iscoroutinefunction(
            service._execute_parallel_search
        ), "_execute_parallel_search() should be sync"


class TestMultiSearchServiceThreadPoolRetained:
    """Test that ThreadPoolExecutor is retained for internal parallelism (AC1)."""

    def test_thread_executor_still_exists(self):
        """MultiSearchService should still have ThreadPoolExecutor.

        AC1: Keep internal ThreadPoolExecutor for parallel multi-repo search
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        assert hasattr(service, "thread_executor")
        assert service.thread_executor is not None
        assert service.thread_executor._max_workers == 5

    def test_thread_executor_used_for_parallel_search(self):
        """ThreadPoolExecutor should be used for parallel multi-repo search.

        AC1: Keep internal ThreadPoolExecutor for parallel multi-repo search
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService
        from concurrent.futures import ThreadPoolExecutor

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        # Verify service has a ThreadPoolExecutor
        assert isinstance(service.thread_executor, ThreadPoolExecutor)

        request = MultiSearchRequest(
            repositories=["repo1", "repo2"],
            query="test",
            search_type="semantic",
            limit=10,
        )

        # Mock internal search method to avoid actual search
        # The ThreadPoolExecutor will still submit tasks, but they will return quickly
        with patch.object(
            service,
            "_search_single_repo_sync",
            return_value=[{"file_path": "test.py", "score": 0.9}],
        ):
            response = service.search(request)

            # Should have results from both repos (ThreadPoolExecutor worked)
            assert "repo1" in response.results
            assert "repo2" in response.results


class TestMultiSearchServiceFunctionalityPreserved:
    """Test that all functionality works correctly after sync conversion."""

    def test_semantic_search_works_sync(self):
        """Semantic search should work synchronously.

        AC1: All existing MultiSearchService tests should pass
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="authentication",
            search_type="semantic",
            limit=10,
        )

        # Mock internal search to avoid actual search
        with patch.object(
            service,
            "_search_single_repo_sync",
            return_value=[{"file_path": "test.py", "score": 0.9}],
        ):
            response = service.search(request)

            assert response.metadata.total_repos_searched == 1
            assert "repo1" in response.results

    def test_fts_search_works_sync(self):
        """FTS search should work synchronously."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="def authenticate",
            search_type="fts",
            limit=10,
        )

        with patch.object(
            service,
            "_search_single_repo_sync",
            return_value=[],
        ):
            response = service.search(request)
            assert response.metadata.total_repos_searched >= 0

    def test_regex_search_works_sync(self):
        """Regex search should work synchronously."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        request = MultiSearchRequest(
            repositories=["repo1"],
            query="def.*",
            search_type="regex",
            limit=10,
        )

        with patch.object(
            service,
            "_search_single_repo_subprocess",
            return_value=[],
        ):
            response = service.search(request)
            assert response is not None

    def test_partial_failure_handling_sync(self):
        """Partial failures should be handled correctly in sync mode.

        AC1: All existing functionality should work
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        config = MultiSearchConfig(max_workers=5, query_timeout_seconds=30)
        service = MultiSearchService(config)

        request = MultiSearchRequest(
            repositories=["good_repo", "bad_repo"],
            query="test",
            search_type="semantic",
            limit=10,
        )

        def mock_search(repo_id, request):
            if repo_id == "bad_repo":
                raise Exception("Repo not found")
            return [{"file_path": "test.py", "score": 0.9}]

        with patch.object(
            service, "_search_single_repo_sync", side_effect=mock_search
        ):
            response = service.search(request)

            # Good repo should have results
            assert "good_repo" in response.results
            # Bad repo should have error
            assert response.errors is not None
            assert "bad_repo" in response.errors
