"""
TDD tests for Story #51: Thread Pool-Enabled MCP Handlers - Sync Verification.

Verifies that key handlers have been converted from def to def,
enabling FastAPI to run them in its thread pool for true concurrent execution.

Written following TDD methodology - tests first, implementation second.
"""

import inspect


class TestSearchHandlersSyncConversion:
    """Test that search handlers are synchronous (AC2)."""

    def test_omni_search_code_is_synchronous(self):
        """_omni_search_code should be a sync function (AC2).

        This is the key search handler for multi-repo search.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code

        assert not inspect.iscoroutinefunction(
            _omni_search_code
        ), "_omni_search_code should be sync, not async"

    def test_search_code_handler_exists(self):
        """search_code handler should exist in handlers module.

        Note: search_code itself may remain async if it calls other async code
        (like RegexSearchService), but _omni_search_code path should be sync.
        """
        from code_indexer.server.mcp.handlers import search_code

        # Just verify it exists - it may still be async for other code paths
        assert callable(search_code), "search_code should be callable"


class TestMultiSearchServiceSyncConversion:
    """Test that MultiSearchService is synchronous (AC1)."""

    def test_multi_search_service_search_is_synchronous(self):
        """MultiSearchService.search() should be synchronous (AC1)."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        assert not inspect.iscoroutinefunction(
            MultiSearchService.search
        ), "MultiSearchService.search should be sync"

    def test_multi_search_service_internal_methods_sync(self):
        """MultiSearchService internal methods should be synchronous (AC1)."""
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        assert not inspect.iscoroutinefunction(
            MultiSearchService._search_threaded
        ), "_search_threaded should be sync"

        assert not inspect.iscoroutinefunction(
            MultiSearchService._search_regex_subprocess
        ), "_search_regex_subprocess should be sync"

        assert not inspect.iscoroutinefunction(
            MultiSearchService._execute_parallel_search
        ), "_execute_parallel_search should be sync"


class TestCacheLayerSyncConversion:
    """Test that cache layer is synchronous (prerequisite from Story #50)."""

    def test_payload_cache_methods_are_sync(self):
        """PayloadCache methods should be synchronous (Story #50)."""
        from code_indexer.server.cache.payload_cache import PayloadCache

        # All key methods should be sync
        assert not inspect.iscoroutinefunction(
            PayloadCache.store
        ), "PayloadCache.store should be sync"

        assert not inspect.iscoroutinefunction(
            PayloadCache.retrieve
        ), "PayloadCache.retrieve should be sync"

        assert not inspect.iscoroutinefunction(
            PayloadCache.truncate_result
        ), "PayloadCache.truncate_result should be sync"

    def test_truncation_helper_methods_are_sync(self):
        """TruncationHelper methods should be synchronous (Story #50)."""
        from code_indexer.server.cache.truncation_helper import TruncationHelper

        assert not inspect.iscoroutinefunction(
            TruncationHelper.truncate_and_cache
        ), "TruncationHelper.truncate_and_cache should be sync"


class TestTruncationFunctionsSyncConversion:
    """Test that truncation functions in handlers are synchronous (Story #50)."""

    def test_apply_payload_truncation_is_sync(self):
        """_apply_payload_truncation should be synchronous (Story #50)."""
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        assert not inspect.iscoroutinefunction(
            _apply_payload_truncation
        ), "_apply_payload_truncation should be sync"

    def test_apply_fts_payload_truncation_is_sync(self):
        """_apply_fts_payload_truncation should be synchronous (Story #50)."""
        from code_indexer.server.mcp.handlers import _apply_fts_payload_truncation

        assert not inspect.iscoroutinefunction(
            _apply_fts_payload_truncation
        ), "_apply_fts_payload_truncation should be sync"

    def test_apply_scip_payload_truncation_is_sync(self):
        """_apply_scip_payload_truncation should be synchronous (Story #50)."""
        from code_indexer.server.mcp.handlers import _apply_scip_payload_truncation

        assert not inspect.iscoroutinefunction(
            _apply_scip_payload_truncation
        ), "_apply_scip_payload_truncation should be sync"


class TestHandlersSyncCallability:
    """Test that sync handlers can be called without await."""

    def test_omni_search_code_returns_dict_directly(self):
        """_omni_search_code should return dict directly, not coroutine.

        This verifies the function is truly sync and not a generator or async.
        """
        from code_indexer.server.mcp.handlers import _omni_search_code
        from unittest.mock import Mock, patch
        from datetime import datetime
        from code_indexer.server.auth.user_manager import User, UserRole
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        mock_user = User(
            username="test",
            password_hash="hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(),
        )

        params = {
            "query_text": "test",
            "repository_alias": ["repo1-global"],
            "limit": 10,
        }

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config:
            mock_service = Mock()
            mock_limits = Mock()
            mock_limits.multi_search_max_workers = 4
            mock_limits.multi_search_timeout_seconds = 30
            mock_config_obj = Mock()
            mock_config_obj.multi_search_limits_config = mock_limits
            mock_service.get_config.return_value = mock_config_obj
            mock_config.return_value = mock_service

            with patch(
                "code_indexer.server.mcp.handlers._expand_wildcard_patterns"
            ) as mock_expand:
                mock_expand.side_effect = lambda x: x

                with patch(
                    "code_indexer.server.multi.multi_search_service.MultiSearchService"
                ) as mock_ms:
                    mock_instance = Mock()
                    mock_response = MultiSearchResponse(
                        results={"repo1-global": []},
                        metadata=MultiSearchMetadata(
                            total_results=0,
                            total_repos_searched=1,
                            execution_time_ms=50,
                        ),
                        errors=None,
                    )
                    mock_instance.search = Mock(return_value=mock_response)
                    mock_ms.return_value = mock_instance

                    # Call should return dict directly, not coroutine
                    result = _omni_search_code(params, mock_user)

                    # Should be a dict, not a coroutine
                    assert isinstance(
                        result, dict
                    ), f"Expected dict, got {type(result)}"
                    assert "content" in result
