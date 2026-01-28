"""
Integration tests for file content truncation with cache retrieval.

Story #33: File Content Returns Cache Handle on Truncation
Tests AC3: User can retrieve full content via get_cached_content
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast
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


def _collect_paginated_content(handlers, cache_handle: str, user, initial: str):
    """Collect all pages of content from cache."""
    all_content = initial
    page = 0

    while True:
        page += 1
        response = handlers.handle_get_cached_content(
            {"handle": cache_handle, "page": page}, user
        )
        data = _extract_response_data(response)
        if not data.get("success"):
            break
        all_content += data.get("content", "")
        if not data.get("has_more", False):
            break

    return all_content


class TestCacheRetrievalIntegration:
    """Test complete flow: truncate file -> retrieve via cache handle (AC3)."""

    @pytest.fixture
    def setup_with_cache(self, tmp_path: Path):
        """Set up test environment with real PayloadCache."""
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        db_path = tmp_path / "test_cache.db"
        cache_config = PayloadCacheConfig(
            preview_size_chars=200,
            max_fetch_size_chars=500,
            cache_ttl_seconds=3600,
        )
        payload_cache = PayloadCache(db_path, cache_config)
        payload_cache.initialize()

        content_limits = ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=50,  # 200 chars max
        )

        return payload_cache, content_limits

    def test_truncated_content_can_be_retrieved_via_cache(
        self, mock_user, setup_with_cache
    ):
        """AC3: Cache handle can retrieve full content after truncation."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.mcp import handlers

        payload_cache, content_limits = setup_with_cache
        large_content = "x" * 500  # 125 tokens, exceeds 50 token limit

        mock_app = MagicMock()
        mock_service = MagicMock()
        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {"size": 500, "path": "large_file.py"},
        }
        mock_app.file_service = mock_service
        mock_app.app.state.payload_cache = payload_cache

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value.content_limits_config = (
            content_limits
        )

        with (
            patch("code_indexer.server.mcp.handlers.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            # Step 1: Get truncated file content
            file_response = handlers.get_file_content(
                {"repository_alias": "test-repo", "file_path": "large_file.py"},
                mock_user,
            )
            file_data = _extract_response_data(file_response)

            assert file_data.get("truncated") is True
            cache_handle = file_data.get("cache_handle")
            assert cache_handle is not None

            # Step 2: Retrieve full content via cache handle
            cache_response = handlers.handle_get_cached_content(
                {"handle": cache_handle}, mock_user
            )
            cache_data = _extract_response_data(cache_response)
            assert cache_data.get("success") is True

            # Collect all paginated content
            initial_content = cache_data.get("content", "")
            if cache_data.get("has_more", False):
                all_content = _collect_paginated_content(
                    handlers, cache_handle, mock_user, initial_content
                )
            else:
                all_content = initial_content

            assert all_content == large_content

    def test_non_truncated_content_has_no_cache_handle(
        self, mock_user, setup_with_cache
    ):
        """AC4: Non-truncated content has cache_handle=null."""
        from unittest.mock import MagicMock, patch
        from code_indexer.server.mcp import handlers

        payload_cache, content_limits = setup_with_cache
        small_content = "def hello(): pass"  # ~4 tokens, under 50 limit

        mock_app = MagicMock()
        mock_service = MagicMock()
        mock_service.get_file_content.return_value = {
            "content": small_content,
            "metadata": {"size": 17, "path": "small_file.py"},
        }
        mock_app.file_service = mock_service
        mock_app.app.state.payload_cache = payload_cache

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value.content_limits_config = (
            content_limits
        )

        with (
            patch("code_indexer.server.mcp.handlers.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            response = handlers.get_file_content(
                {"repository_alias": "test-repo", "file_path": "small_file.py"},
                mock_user,
            )
            data = _extract_response_data(response)

            assert data.get("truncated") is False
            assert data.get("cache_handle") is None

            content_blocks = data.get("content", [])
            assert len(content_blocks) > 0
            assert content_blocks[0].get("text") == small_content
