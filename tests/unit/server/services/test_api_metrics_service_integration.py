"""
Unit tests for Story #4 Service-Level API Metrics Instrumentation.

Tests that API metrics are correctly incremented at the SERVICE layer (not protocol layer)
so both MCP and REST API calls get tracked.

Services that need instrumentation:
- SemanticSearchService.search_repository_path() -> semantic searches
- SemanticQueryManager._perform_search() -> semantic/FTS/hybrid/temporal searches
- RegexSearchService.search() -> regex searches

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
from contextlib import ExitStack
from unittest.mock import patch, AsyncMock
from pathlib import Path


class TestSemanticSearchServiceMetrics:
    """Test that SemanticSearchService does NOT track metrics (to prevent double-counting).

    Metrics tracking happens at the MCP/REST entry point layer
    (semantic_query_manager._perform_search), NOT in SemanticSearchService.
    Having metrics tracking in both places caused each search to be counted twice.
    """

    def test_search_repository_path_does_not_track_metrics(self):
        """Test that search_repository_path does NOT increment metrics.

        ARCHITECTURE FIX: Metrics were previously tracked in BOTH:
        1. semantic_query_manager._perform_search() (CORRECT - keep this)
        2. search_service.search_repository_path() (INCORRECT - removed)

        This caused double-counting - each semantic search was counted twice.
        Now metrics are ONLY tracked at the entry point layer.
        """
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from code_indexer.server.services.search_service import SemanticSearchService
        from code_indexer.server.models.api_models import SemanticSearchRequest

        # Reset metrics to known state
        api_metrics_service.reset()

        # Create service
        service = SemanticSearchService()

        # Mock _perform_semantic_search to return empty results (allows completion)
        # and mock os.path.exists to avoid real filesystem checks
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(service, "_perform_semantic_search", return_value=[])
            )
            stack.enter_context(patch("os.path.exists", return_value=True))

            # Create request
            request = SemanticSearchRequest(
                query="test query",
                limit=10,
                include_source=False,
            )

            # Execute search - should complete successfully with mocked internals
            result = service.search_repository_path("/fake/repo/path", request)

            # Verify search completed
            assert result is not None
            assert result.results == []

        # Verify NO metrics were incremented by search_repository_path
        # Metrics tracking is done at the MCP entry point layer
        # (semantic_query_manager._perform_search), not here
        metrics = api_metrics_service.get_metrics()
        assert metrics["semantic_searches"] == 0, (
            f"Expected 0 semantic searches (should not track here), got {metrics['semantic_searches']}. "
            "search_repository_path() should NOT track metrics - that causes double-counting. "
            "Metrics are tracked in semantic_query_manager._perform_search() instead."
        )
        assert metrics["other_index_searches"] == 0
        assert metrics["regex_searches"] == 0


class TestRegexSearchServiceMetrics:
    """Test that RegexSearchService increments regex_search metrics."""

    @pytest.mark.asyncio
    async def test_regex_search_increments_regex_search_metric(self):
        """Test that RegexSearchService.search() increments regex_search counter.

        Story #4 Fix: Metrics should be tracked at service layer so both
        MCP and REST APIs get metrics tracked.
        """
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from code_indexer.global_repos.regex_search import RegexSearchService

        # Reset metrics to known state
        api_metrics_service.reset()

        with ExitStack() as stack:
            # Mock the search engine detection
            stack.enter_context(
                patch.object(
                    RegexSearchService, "_detect_search_engine", return_value="ripgrep"
                )
            )

            service = RegexSearchService(Path("/fake/repo"))

            # Mock _search_ripgrep to return empty results (allows completion)
            stack.enter_context(
                patch.object(
                    service,
                    "_search_ripgrep",
                    new_callable=AsyncMock,
                    return_value=([], 0),
                )
            )

            # Mock path existence check
            stack.enter_context(patch.object(Path, "exists", return_value=True))

            # Execute search - should complete successfully
            result = await service.search(pattern="test", max_results=10)

            # Verify search completed
            assert result is not None
            assert result.matches == []

        # Verify regex search metric was incremented
        metrics = api_metrics_service.get_metrics()
        assert metrics["regex_searches"] == 1, (
            f"Expected 1 regex search, got {metrics['regex_searches']}. "
            "Service-level instrumentation missing in RegexSearchService.search()"
        )
        assert metrics["semantic_searches"] == 0
        assert metrics["other_index_searches"] == 0


class TestSemanticQueryManagerMetrics:
    """Test that SemanticQueryManager increments appropriate metrics based on search_mode.

    These tests verify that metrics are incremented at the START of _perform_search,
    before any actual search execution happens. This ensures metrics are always
    counted even if the search fails.
    """

    def _create_mock_manager_and_repos(self):
        """Helper to create a mocked SemanticQueryManager with test repos."""
        from code_indexer.server.query.semantic_query_manager import (
            SemanticQueryManager,
        )

        manager = SemanticQueryManager()
        user_repos = [
            {
                "user_alias": "test-repo",
                "repo_path": "/fake/path",
                "actual_repo_id": "test",
            }
        ]
        return manager, user_repos

    def test_perform_search_semantic_mode_increments_semantic_search(self):
        """Test that _perform_search with semantic mode increments semantic_search counter."""
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from code_indexer.server.query.semantic_query_manager import QueryResult

        # Reset metrics
        api_metrics_service.reset()

        manager, user_repos = self._create_mock_manager_and_repos()

        # Mock at a level that allows complete execution
        mock_result = QueryResult(
            file_path="test.py",
            line_number=1,
            code_snippet="test code",
            similarity_score=0.9,
            repository_alias="test-repo",
        )

        with patch.object(
            manager, "_search_single_repository", return_value=[mock_result]
        ):
            # Execute search with semantic mode - should complete
            results = manager._perform_search(
                username="testuser",
                user_repos=user_repos,
                query_text="test query",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                search_mode="semantic",
            )

            # Verify search completed
            assert results is not None

        # Verify semantic search metric was incremented
        metrics = api_metrics_service.get_metrics()
        assert metrics["semantic_searches"] == 1, (
            f"Expected 1 semantic search, got {metrics['semantic_searches']}. "
            "Service-level instrumentation missing in _perform_search()"
        )
        assert metrics["other_index_searches"] == 0

    def test_perform_search_fts_mode_increments_other_index_search(self):
        """Test that _perform_search with FTS mode increments other_index_searches counter."""
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from code_indexer.server.query.semantic_query_manager import QueryResult

        # Reset metrics
        api_metrics_service.reset()

        manager, user_repos = self._create_mock_manager_and_repos()

        mock_result = QueryResult(
            file_path="test.py",
            line_number=1,
            code_snippet="test code",
            similarity_score=0.9,
            repository_alias="test-repo",
        )

        with patch.object(manager, "_search_single_repository", return_value=[mock_result]):
            results = manager._perform_search(
                username="testuser",
                user_repos=user_repos,
                query_text="test query",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                search_mode="fts",
            )

            assert results is not None

        # Verify other_index_searches metric was incremented
        metrics = api_metrics_service.get_metrics()
        assert metrics["other_index_searches"] == 1, (
            f"Expected 1 other index search, got {metrics['other_index_searches']}. "
            "Service-level instrumentation missing in _perform_search() for FTS mode"
        )
        assert metrics["semantic_searches"] == 0

    def test_perform_search_hybrid_mode_increments_other_index_search(self):
        """Test that _perform_search with hybrid mode increments other_index_searches counter."""
        from code_indexer.server.services.api_metrics_service import (
            api_metrics_service,
        )
        from code_indexer.server.query.semantic_query_manager import QueryResult

        # Reset metrics
        api_metrics_service.reset()

        manager, user_repos = self._create_mock_manager_and_repos()

        mock_result = QueryResult(
            file_path="test.py",
            line_number=1,
            code_snippet="test code",
            similarity_score=0.9,
            repository_alias="test-repo",
        )

        with patch.object(manager, "_search_single_repository", return_value=[mock_result]):
            results = manager._perform_search(
                username="testuser",
                user_repos=user_repos,
                query_text="test query",
                limit=10,
                min_score=0.5,
                file_extensions=None,
                search_mode="hybrid",
            )

            assert results is not None

        metrics = api_metrics_service.get_metrics()
        assert metrics["other_index_searches"] == 1, (
            f"Expected 1 other index search, got {metrics['other_index_searches']}. "
            "Service-level instrumentation missing in _perform_search() for hybrid mode"
        )
        assert metrics["semantic_searches"] == 0


class TestNoDoubleCountingAfterFix:
    """Verify that after moving instrumentation to service layer, metrics are not double-counted.

    The protocol.py file has tools_with_own_metrics = {"search_code", "regex_search"}
    to skip "other API calls" for these tools. After the fix, the MCP handlers
    should NOT call api_metrics_service.increment_* for search operations - that
    should only happen at the service layer.
    """

    def test_protocol_excludes_search_tools_from_other_api_calls(self):
        """Verify that protocol.py correctly excludes search tools from other_api_calls.

        This test verifies the existing exclusion list is correct, ensuring that
        search_code and regex_search are not double-counted as "other_api_calls".
        """
        from code_indexer.server.mcp import protocol

        # Read the source to verify the exclusion set exists
        # We check the module has the expected handling
        import inspect

        source = inspect.getsource(protocol.handle_tools_call)

        # Verify the exclusion list is present
        assert "tools_with_own_metrics" in source, (
            "protocol.py should have tools_with_own_metrics set to exclude "
            "search tools from other_api_calls counting"
        )
        assert "search_code" in source, (
            "search_code should be in tools_with_own_metrics exclusion"
        )
        assert "regex_search" in source, (
            "regex_search should be in tools_with_own_metrics exclusion"
        )
