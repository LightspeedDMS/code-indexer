"""Unit tests for _omni_search_code explicit truncation after aggregation.

Bug Fix for Story #683: MCP _omni_search_code Missing Payload Truncation
Story #36: Updated to work with MultiSearchService delegation pattern.
Story #50: Updated truncation function mocks to sync (truncation helpers are now sync).

TDD methodology: Tests written BEFORE the fix is implemented.
"""

import json
import pytest
from unittest.mock import patch, Mock, AsyncMock


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    from datetime import datetime
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="test_user",
        password_hash="dummy_hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def setup_payload_cache(cache_100_chars):
    """Set up and tear down payload cache on app state."""
    from code_indexer.server import app as app_module

    original = getattr(app_module.app.state, "payload_cache", None)
    app_module.app.state.payload_cache = cache_100_chars
    yield cache_100_chars
    if original is None:
        if hasattr(app_module.app.state, "payload_cache"):
            delattr(app_module.app.state, "payload_cache")
    else:
        app_module.app.state.payload_cache = original


@pytest.fixture
def mock_config_service():
    """Mock ConfigService with multi_search_limits_config."""
    from code_indexer.server.mcp import handlers

    with patch.object(handlers, "get_config_service") as mock:
        mock_service = Mock()
        mock_config = Mock()
        mock_limits = Mock()
        # Use correct attribute names matching MultiSearchLimitsConfig
        mock_limits.multi_search_max_workers = 4
        mock_limits.multi_search_timeout_seconds = 30
        mock_config.multi_search_limits_config = mock_limits
        mock_service.get_config.return_value = mock_config
        mock.return_value = mock_service
        yield mock


class TestOmniSearchAppliesTruncation:
    """Tests verifying _omni_search_code applies truncation after aggregation.

    Story #36: Updated to mock MultiSearchService instead of search_code.
    Story #50: Truncation functions are now sync, mocks updated accordingly.
    """

    @pytest.mark.asyncio
    async def test_semantic_truncation_applied_to_aggregated_results(
        self, setup_payload_cache, mock_user, mock_config_service
    ):
        """_omni_search_code applies _apply_payload_truncation for semantic mode."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        truncation_calls = []
        original_fn = handlers._apply_payload_truncation

        # Story #50: Tracking function is now sync (truncation helpers are sync)
        def tracking_fn(results):
            truncation_calls.append(len(results))
            return original_fn(results)

        # Mock MultiSearchService to return results
        service_results = {
            "repo-alpha-global": [
                {"file_path": "/src/a.py", "content": "A" * 200, "score": 0.92}
            ],
            "repo-beta-global": [
                {"file_path": "/src/b.py", "content": "B" * 200, "score": 0.88}
            ],
        }

        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch.object(
                handlers, "_apply_payload_truncation", side_effect=tracking_fn
            ),
            patch.object(
                handlers, "_expand_wildcard_patterns", side_effect=lambda x: x
            ),
        ):
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results=service_results,
                metadata=MultiSearchMetadata(
                    total_results=2, total_repos_searched=2, execution_time_ms=100
                ),
                errors=None,
            )
            mock_service.search = AsyncMock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            params = {
                "repository_alias": ["repo-alpha-global", "repo-beta-global"],
                "query_text": "test",
                "search_mode": "semantic",
                "limit": 10,
            }
            await handlers._omni_search_code(params, mock_user)

        assert len(truncation_calls) > 0, "Semantic truncation should be called"
        assert truncation_calls[-1] == 2, "Should truncate 2 aggregated results"

    @pytest.mark.asyncio
    async def test_fts_truncation_applied_for_fts_mode(
        self, setup_payload_cache, mock_user, mock_config_service
    ):
        """_omni_search_code applies _apply_fts_payload_truncation for FTS mode."""
        from code_indexer.server.mcp import handlers
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        truncation_calls = []
        original_fn = handlers._apply_fts_payload_truncation

        # Story #50: Tracking function is now sync (truncation helpers are sync)
        def tracking_fn(results):
            truncation_calls.append(len(results))
            return original_fn(results)

        # Mock MultiSearchService to return results
        service_results = {
            "repo-alpha-global": [
                {"file_path": "/src/a.py", "code_snippet": "S" * 200, "score": 0.92}
            ],
        }

        with (
            patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class,
            patch.object(
                handlers, "_apply_fts_payload_truncation", side_effect=tracking_fn
            ),
            patch.object(
                handlers, "_expand_wildcard_patterns", side_effect=lambda x: x
            ),
        ):
            mock_service = Mock()
            mock_response = MultiSearchResponse(
                results=service_results,
                metadata=MultiSearchMetadata(
                    total_results=1, total_repos_searched=1, execution_time_ms=50
                ),
                errors=None,
            )
            mock_service.search = AsyncMock(return_value=mock_response)
            mock_service_class.return_value = mock_service

            params = {
                "repository_alias": ["repo-alpha-global"],
                "query_text": "test",
                "search_mode": "fts",
                "limit": 10,
            }
            await handlers._omni_search_code(params, mock_user)

        assert len(truncation_calls) > 0, "FTS truncation should be called for FTS mode"
