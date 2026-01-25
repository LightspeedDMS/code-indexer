"""
Unit tests for get_file_content MCP handler truncation with cache handle support.

Story #33: File Content Returns Cache Handle on Truncation
Tests AC1 and AC4:
- AC1: get_file_content returns cache_handle when content exceeds token limit
- AC4: Backward compatible response format
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock, AsyncMock
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


class TestGetFileContentTruncation:
    """Test get_file_content handler truncation with cache handle (AC1)."""

    @pytest.fixture
    def mock_file_service(self):
        """Create mock FileListingService."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_service = MagicMock()
            mock_app.file_service = mock_service
            yield mock_service, mock_app

    @pytest.mark.asyncio
    async def test_truncated_content_returns_cache_handle(
        self, mock_user, mock_file_service
    ):
        """AC1: Truncated content returns cache_handle field."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        # Simulate large file content from file service
        large_content = "x" * 10000  # Large content

        mock_service.get_file_content.return_value = {
            "content": large_content,
            "metadata": {
                "size": 10000,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "large_file.py",
                "total_lines": 500,
                "returned_lines": 500,
                "offset": 1,
                "limit": None,
                "has_more": False,
            },
        }

        # Mock payload cache for truncation
        mock_cache = AsyncMock()
        mock_cache.store = AsyncMock(return_value="cache_handle_abc123")
        mock_cache.config.max_fetch_size_chars = 500  # Required for total_pages calculation
        mock_app.app.state.payload_cache = mock_cache

        # Mock content limits config
        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100  # 400 chars max with default 4 chars/token
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = 4
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "large_file.py",
            }

            mcp_response = await handlers.get_file_content(params, mock_user)

            data = _extract_response_data(mcp_response)

            # Verify response includes truncation fields
            assert data.get("success") is True
            assert "cache_handle" in data or "cache_handle" in data.get("metadata", {})
            assert "truncated" in data or "truncated" in data.get("metadata", {})

    @pytest.mark.asyncio
    async def test_small_content_no_cache_handle(self, mock_user, mock_file_service):
        """AC4: Small content has cache_handle=null and truncated=false."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        # Simulate small file content
        small_content = "def hello(): pass"

        mock_service.get_file_content.return_value = {
            "content": small_content,
            "metadata": {
                "size": 17,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "small_file.py",
                "total_lines": 1,
                "returned_lines": 1,
                "offset": 1,
                "limit": None,
                "has_more": False,
            },
        }

        # Mock payload cache (should not be called for small content)
        mock_cache = AsyncMock()
        mock_cache.store = AsyncMock()
        mock_app.app.state.payload_cache = mock_cache

        # Mock content limits config
        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_config_service:
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                50000
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = 4
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-repo",
                "file_path": "small_file.py",
            }

            mcp_response = await handlers.get_file_content(params, mock_user)

            data = _extract_response_data(mcp_response)

            # Verify response has success
            assert data.get("success") is True

            # Content should be present
            assert "content" in data or len(mcp_response.get("content", [])) > 0


class TestGetFileContentBackwardCompatibility:
    """Test backward compatible response format (AC4)."""

    @pytest.fixture
    def mock_file_service(self):
        """Create mock FileListingService."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_service = MagicMock()
            mock_app.file_service = mock_service
            yield mock_service, mock_app

    @pytest.mark.asyncio
    async def test_content_field_always_present(self, mock_user, mock_file_service):
        """AC4: Content field is always present (full content or preview)."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        mock_service.get_file_content.return_value = {
            "content": "file content here",
            "metadata": {
                "size": 17,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "test.py",
            },
        }

        params = {
            "repository_alias": "test-repo",
            "file_path": "test.py",
        }

        mcp_response = await handlers.get_file_content(params, mock_user)

        # MCP response should have content blocks
        assert "content" in mcp_response
        assert len(mcp_response["content"]) > 0

    @pytest.mark.asyncio
    async def test_existing_clients_continue_to_work(self, mock_user, mock_file_service):
        """AC4: Existing clients that ignore new fields continue to work."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_file_service

        mock_service.get_file_content.return_value = {
            "content": "# File content\ndef main(): pass",
            "metadata": {
                "size": 32,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "main.py",
                "total_lines": 2,
                "returned_lines": 2,
                "offset": 1,
                "limit": None,
                "has_more": False,
            },
        }

        params = {
            "repository_alias": "test-repo",
            "file_path": "main.py",
        }

        mcp_response = await handlers.get_file_content(params, mock_user)

        # Existing clients expect content blocks with type and text
        assert "content" in mcp_response
        content_blocks = mcp_response["content"]
        assert len(content_blocks) > 0
        assert content_blocks[0]["type"] == "text"
        assert "text" in content_blocks[0]

        # Parse the text content as JSON
        data = json.loads(content_blocks[0]["text"])

        # Old fields should still be present
        assert data.get("success") is True
        assert "content" in data or "metadata" in data


class TestGetFileContentGlobalRepoTruncation:
    """Test get_file_content handler truncation for global repos (AC2).

    AC2: get_file_content_by_path works for global repos (ends with -global suffix).
    Verifies that global repo path resolution and truncation work together.
    """

    @pytest.fixture
    def mock_global_repo_setup(self):
        """Set up mocks for global repo path resolution."""
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_service = MagicMock()
            mock_app.file_service = mock_service
            mock_app.app.state.golden_repos_dir = "/mock/golden-repos"
            yield mock_service, mock_app

    @pytest.mark.asyncio
    async def test_global_repo_truncation_applies_cache_handle(
        self, mock_user, mock_global_repo_setup
    ):
        """AC2: Global repo path uses get_file_content_by_path with truncation."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_global_repo_setup

        # Large content that will be truncated
        large_content = "x" * 10000

        mock_service.get_file_content_by_path.return_value = {
            "content": large_content,
            "metadata": {
                "size": 10000,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "large_file.py",
            },
        }

        # Mock global registry
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "test-global", "repo_name": "test", "index_path": "/mock/path"}
        ]

        # Mock alias manager
        mock_alias_manager = MagicMock()
        mock_alias_manager.read_alias.return_value = "/mock/test/repo/path"

        # Mock payload cache
        mock_cache = AsyncMock()
        mock_cache.store = AsyncMock(return_value="global_cache_handle_xyz")
        mock_cache.config.max_fetch_size_chars = 500  # Required for total_pages calculation
        mock_app.app.state.payload_cache = mock_cache

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager,
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_service,
        ):
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                100
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = 4
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-global",  # Global repo suffix
                "file_path": "large_file.py",
            }

            mcp_response = await handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            # Verify global repo path was used
            mock_service.get_file_content_by_path.assert_called_once()

            # Verify truncation was applied
            assert data.get("success") is True
            cache_handle = data.get("cache_handle") or data.get("metadata", {}).get(
                "cache_handle"
            )
            assert cache_handle == "global_cache_handle_xyz"

    @pytest.mark.asyncio
    async def test_global_repo_small_file_no_truncation(
        self, mock_user, mock_global_repo_setup
    ):
        """AC2: Global repo small file returns no cache_handle."""
        from code_indexer.server.mcp import handlers

        mock_service, mock_app = mock_global_repo_setup

        # Small content - no truncation needed
        small_content = "def hello(): pass"

        mock_service.get_file_content_by_path.return_value = {
            "content": small_content,
            "metadata": {
                "size": 17,
                "modified_at": "2025-12-29T12:00:00Z",
                "language": "python",
                "path": "small_file.py",
            },
        }

        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {"alias_name": "test-global", "repo_name": "test", "index_path": "/mock/path"}
        ]

        mock_alias_manager = MagicMock()
        mock_alias_manager.read_alias.return_value = "/mock/test/repo/path"

        mock_cache = AsyncMock()
        mock_app.app.state.payload_cache = mock_cache

        with (
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager,
            ),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_service,
        ):
            mock_config = MagicMock()
            mock_config.get_config.return_value.content_limits_config.file_content_max_tokens = (
                50000
            )
            mock_config.get_config.return_value.content_limits_config.chars_per_token = 4
            mock_config_service.return_value = mock_config

            params = {
                "repository_alias": "test-global",
                "file_path": "small_file.py",
            }

            mcp_response = await handlers.get_file_content(params, mock_user)
            data = _extract_response_data(mcp_response)

            assert data.get("success") is True

            # Small file should not be truncated
            truncated = data.get("truncated") or data.get("metadata", {}).get("truncated")
            assert truncated is False

            # No cache_handle for small files
            cache_handle = data.get("cache_handle") or data.get("metadata", {}).get(
                "cache_handle"
            )
            assert cache_handle is None
