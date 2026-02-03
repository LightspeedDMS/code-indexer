"""
Unit tests for get_file_content MCP handler returning pagination info (total_pages, has_more).

Story #34: Add missing pagination information in TruncationHelper and get_file_content response.

When get_file_content returns a cache_handle for truncated content, it MUST also include:
- total_pages: How many pages the cached content will have
- has_more: Whether there's more content to retrieve (True when truncated)

This information appears in BOTH metadata and top-level response for backward compatibility.
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock, Mock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _extract_response_data(mcp_response: dict) -> dict:
    """Extract actual response data from MCP wrapper."""
    if "content" in mcp_response and len(mcp_response["content"]) > 0:
        content = mcp_response["content"][0]
        if "text" in content:
            try:
                return cast(dict, json.loads(content["text"]))
            except json.JSONDecodeError:
                return {"text": content["text"]}
    return mcp_response


class TestGetFileContentPaginationInfo:
    """Test get_file_content handler returns pagination info for truncated content."""

    @pytest.fixture
    def mock_file_service(self):
        """Create mock FileListingService."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_service = MagicMock()
            mock_app.file_service = mock_service
            yield mock_service, mock_app

    def test_truncated_content_returns_total_pages_in_metadata(
        self, mock_user, mock_file_service
    ):
        """Truncated content metadata must include total_pages."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        # Large content that will be truncated
        large_content = "x" * 10000

        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {
                "size": 10000,
                "path": "large_file.py",
            },
        }

        # Mock payload cache (Epic #48: sync, not async)
        mock_cache = MagicMock()
        mock_cache.store = Mock(return_value="cache_handle_abc123")
        # max_fetch_size_chars determines pages: 10000 / 500 = 20 pages
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        # Mock content limits config
        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "large_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            # Verify metadata includes total_pages
            metadata = data.get("metadata", {})
            assert "total_pages" in metadata, "metadata must include total_pages"
            assert (
                metadata["total_pages"] > 0
            ), "total_pages must be > 0 for truncated content"

    def test_truncated_content_returns_has_more_in_metadata(
        self, mock_user, mock_file_service
    ):
        """Truncated content metadata must include has_more=True."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        large_content = "x" * 10000

        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {"size": 10000, "path": "large_file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.store = Mock(return_value="cache_handle_abc123")
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "large_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            metadata = data.get("metadata", {})
            assert "has_more" in metadata, "metadata must include has_more"
            assert (
                metadata["has_more"] is True
            ), "has_more must be True for truncated content"

    def test_truncated_content_returns_total_pages_at_top_level(
        self, mock_user, mock_file_service
    ):
        """Truncated content must include total_pages at top level for flat clients."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        large_content = "x" * 10000

        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {"size": 10000, "path": "large_file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.store = Mock(return_value="cache_handle_abc123")
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "large_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            # Top-level should also have total_pages
            assert "total_pages" in data, "top-level response must include total_pages"
            assert data["total_pages"] > 0

    def test_truncated_content_returns_has_more_at_top_level(
        self, mock_user, mock_file_service
    ):
        """Truncated content must include has_more at top level for flat clients."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        large_content = "x" * 10000

        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {"size": 10000, "path": "large_file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.store = Mock(return_value="cache_handle_abc123")
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "large_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert "has_more" in data, "top-level response must include has_more"
            assert data["has_more"] is True

    def test_small_content_total_pages_zero(self, mock_user, mock_file_service):
        """Non-truncated content should have total_pages=0."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        small_content = "def hello(): pass"  # Small content

        mock_service.get_file_content.return_value = {
            "content": small_content,
            "metadata": {"size": 17, "path": "small_file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                50000
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "small_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            # Non-truncated should have total_pages=0
            total_pages = data.get("total_pages") or data.get("metadata", {}).get(
                "total_pages"
            )
            assert total_pages == 0, "Non-truncated content should have total_pages=0"

    def test_small_content_has_more_false(self, mock_user, mock_file_service):
        """Non-truncated content should have has_more=False."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        small_content = "def hello(): pass"

        mock_service.get_file_content.return_value = {
            "content": small_content,
            "metadata": {"size": 17, "path": "small_file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                50000
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "small_file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            has_more = data.get("has_more")
            if has_more is None:
                has_more = data.get("metadata", {}).get("has_more")
            assert has_more is False, "Non-truncated content should have has_more=False"

    def test_total_pages_calculation_correct(self, mock_user, mock_file_service):
        """Verify total_pages is calculated correctly based on content size and max_fetch_size_chars."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        # 2500 chars with max_fetch_size_chars=500 = 5 pages
        content = "x" * 2500

        mock_service.get_file_content.return_value = {
            "content": content,
            "metadata": {"size": 2500, "path": "file.py"},
        }

        # Epic #48: sync, not async
        mock_cache = MagicMock()
        mock_cache.store = Mock(return_value="cache_handle_123")
        mock_cache.config.max_fetch_size_chars = 500
        mock_app.app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = (
                4
            )
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "file.py",
            }

            mcp_response = handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            total_pages = data.get("total_pages") or data.get("metadata", {}).get(
                "total_pages"
            )
            assert (
                total_pages == 5
            ), f"Expected 5 pages for 2500 chars with 500 chars/page, got {total_pages}"
