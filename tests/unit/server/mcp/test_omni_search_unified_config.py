"""
Tests for unified MCP/REST multi-search configuration.

Story #36 was supposed to unify MCP and REST multi-search, but configuration
was left SEPARATE. These tests verify that MCP uses MultiSearchConfig.from_config()
to ensure both MCP and REST use the same unified settings.

Written following TDD methodology - tests first, implementation second.
"""

import pytest
from datetime import datetime
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    return User(
        username="testuser",
        password_hash="hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_wildcard_expansion():
    """Mock wildcard expansion to return patterns unchanged."""
    with patch("code_indexer.server.mcp.handlers._expand_wildcard_patterns") as mock:
        mock.side_effect = lambda patterns: patterns
        yield mock


class TestMcpUsesUnifiedMultiSearchConfig:
    """Test that MCP uses MultiSearchConfig.from_config() for unified configuration."""

    def test_mcp_calls_multi_search_config_from_config(
        self, mock_user, mock_wildcard_expansion
    ):
        """
        MCP _omni_search_code MUST call MultiSearchConfig.from_config(config_service)
        instead of manually constructing MultiSearchConfig.

        This ensures MCP and REST use the SAME configuration settings:
        - multi_search_max_workers (unified)
        - multi_search_timeout_seconds (unified)

        NOT the separate omni_max_workers/omni_per_repo_timeout_seconds.
        """
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global"],
            "limit": 10,
        }

        # Create mock config service
        mock_config_service = Mock()
        mock_config = Mock()
        mock_limits = Mock()
        # Set DIFFERENT values to prove which one is used
        mock_limits.omni_max_workers = (
            100  # Wrong value (MCP-specific, should NOT be used)
        )
        mock_limits.omni_per_repo_timeout_seconds = 999  # Wrong value
        mock_limits.multi_search_max_workers = 8  # Correct unified value
        mock_limits.multi_search_timeout_seconds = 60  # Correct unified value
        mock_config.multi_search_limits_config = mock_limits
        mock_config_service.get_config.return_value = mock_config

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_config:
            mock_get_config.return_value = mock_config_service

            # Patch MultiSearchConfig.from_config to track if it's called
            with patch(
                "code_indexer.server.multi.multi_search_config.MultiSearchConfig.from_config"
            ) as mock_from_config:
                # Setup the mock to return a real config with unified values
                from code_indexer.server.multi.multi_search_config import (
                    MultiSearchConfig,
                )

                mock_from_config.return_value = MultiSearchConfig(
                    max_workers=8,
                    query_timeout_seconds=60,
                )

                # Patch MultiSearchService to capture the config
                with patch(
                    "code_indexer.server.multi.multi_search_service.MultiSearchService"
                ) as mock_service_class:
                    mock_service = Mock()
                    mock_response = MultiSearchResponse(
                        results={"repo1-global": []},
                        metadata=MultiSearchMetadata(
                            total_results=0,
                            total_repos_searched=1,
                            execution_time_ms=50,
                        ),
                        errors=None,
                    )
                    # Story #51: handlers are now sync
                    mock_service.search = Mock(return_value=mock_response)
                    mock_service_class.return_value = mock_service

                    from code_indexer.server.mcp.handlers import _omni_search_code

                    _omni_search_code(params, mock_user)

                    # CRITICAL ASSERTION: from_config MUST be called with config_service
                    mock_from_config.assert_called_once_with(mock_config_service)

    def test_mcp_uses_unified_config_values(self, mock_user, mock_wildcard_expansion):
        """
        MCP must use unified config values (multi_search_*), not MCP-specific (omni_*).

        This test uses different values for omni_* and multi_search_* settings
        and verifies that the unified multi_search_* values are passed to the service.
        """
        from code_indexer.server.multi.models import (
            MultiSearchResponse,
            MultiSearchMetadata,
        )

        params = {
            "query_text": "authentication",
            "repository_alias": ["repo1-global"],
            "limit": 10,
        }

        # Create mock config service with DIFFERENT values
        mock_config_service = Mock()
        mock_config = Mock()
        mock_limits = Mock()
        # Set DIFFERENT values to prove which one is used
        mock_limits.omni_max_workers = 100  # Wrong (MCP-specific)
        mock_limits.omni_per_repo_timeout_seconds = 999  # Wrong
        mock_limits.multi_search_max_workers = 8  # Correct (unified)
        mock_limits.multi_search_timeout_seconds = 60  # Correct (unified)
        mock_config.multi_search_limits_config = mock_limits
        mock_config_service.get_config.return_value = mock_config

        captured_config = None

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_config:
            mock_get_config.return_value = mock_config_service

            with patch(
                "code_indexer.server.multi.multi_search_service.MultiSearchService"
            ) as mock_service_class:

                def capture_config(config):
                    nonlocal captured_config
                    captured_config = config
                    mock_service = Mock()
                    mock_response = MultiSearchResponse(
                        results={"repo1-global": []},
                        metadata=MultiSearchMetadata(
                            total_results=0,
                            total_repos_searched=1,
                            execution_time_ms=50,
                        ),
                        errors=None,
                    )
                    # Story #51: handlers are now sync
                    mock_service.search = Mock(return_value=mock_response)
                    return mock_service

                mock_service_class.side_effect = capture_config

                from code_indexer.server.mcp.handlers import _omni_search_code

                _omni_search_code(params, mock_user)

        # CRITICAL ASSERTIONS: Must use unified values, not omni-specific
        assert captured_config is not None, "MultiSearchService was not called"
        assert captured_config.max_workers == 8, (
            f"Expected unified max_workers=8, got {captured_config.max_workers}. "
            "MCP is using omni_max_workers instead of multi_search_max_workers!"
        )
        assert captured_config.query_timeout_seconds == 60, (
            f"Expected unified timeout=60, got {captured_config.query_timeout_seconds}. "
            "MCP is using omni_per_repo_timeout_seconds instead of multi_search_timeout_seconds!"
        )


class TestConfigUnificationParity:
    """Test that MCP and REST use identical configuration paths."""

    def test_rest_uses_from_config(self):
        """REST API uses MultiSearchConfig.from_config() - baseline verification."""
        # This is a documentation test showing the expected pattern
        # REST does: MultiSearchConfig.from_config(config_service)
        # MCP should do the same
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig

        mock_config_service = Mock()
        mock_config = Mock()
        mock_limits = Mock()
        mock_limits.multi_search_max_workers = 5
        mock_limits.multi_search_timeout_seconds = 45
        mock_config.multi_search_limits_config = mock_limits
        mock_config_service.get_config.return_value = mock_config

        # REST pattern
        config = MultiSearchConfig.from_config(mock_config_service)

        assert config.max_workers == 5
        assert config.query_timeout_seconds == 45
