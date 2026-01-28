"""
Unit tests for get_file_content MCP handler TruncationHelper integration.

Story #33 Fix: Verify that MCP handler uses skip_truncation=True when calling
FileService, allowing TruncationHelper to handle truncation with cache_handle support.
"""

import json
from datetime import datetime
from typing import cast
from unittest.mock import patch, MagicMock, Mock
import pytest

from code_indexer.server.auth.user_manager import User, UserRole


# Test configuration constants
LOW_TOKEN_LIMIT = 1000  # Triggers truncation (~4000 chars max)
HIGH_TOKEN_LIMIT = 50000  # No truncation
MAX_TOKENS_PER_REQUEST = 5000  # Default FileService token limit
CHARS_PER_TOKEN = 4
SMALL_FILE_LINES = 2
MEDIUM_FILE_LINES = 100
LARGE_FILE_LINES = 1000
HUGE_FILE_LINES = 2000
MAX_FETCH_SIZE_CHARS = 50000


@pytest.fixture
def mock_user():
    """Create mock user for testing."""
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_payload_cache():
    """Create configured mock payload cache.

    Epic #48: Handlers are now sync, so cache must use Mock (not AsyncMock).
    """
    cache = MagicMock()
    cache.store = Mock(return_value="cache-handle-123")
    cache.config = MagicMock()
    cache.config.max_fetch_size_chars = MAX_FETCH_SIZE_CHARS
    return cache


def _make_file_service_response(content, path="test.py", total_lines=None):
    """Create a standard FileService response structure."""
    if total_lines is None:
        total_lines = content.count("\n") or 1
    return {
        "content": content,
        "metadata": {
            "size": len(content),
            "modified_at": "2025-12-29T12:00:00Z",
            "language": "python",
            "path": path,
            "total_lines": total_lines,
            "returned_lines": total_lines,
            "offset": 1,
            "limit": None,
            "has_more": False,
            "truncated": False,
            "truncated_at_line": None,
            "estimated_tokens": len(content) // CHARS_PER_TOKEN,
            "max_tokens_per_request": MAX_TOKENS_PER_REQUEST,
            "requires_pagination": False,
            "pagination_hint": None,
        },
    }


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


def _mock_config_service(token_limit):
    """Create a mock config service context manager."""
    mock_config = MagicMock()
    mock_content_limits = MagicMock()
    mock_content_limits.file_content_max_tokens = token_limit
    mock_content_limits.chars_per_token = CHARS_PER_TOKEN
    mock_config.content_limits_config = mock_content_limits
    return mock_config


class TestMcpHandlerSkipTruncation:
    """Test that MCP handler calls FileService with skip_truncation=True."""

    def test_handler_calls_file_service_with_skip_truncation_true(
        self, mock_user
    ):
        """Verify MCP handler calls get_file_content with skip_truncation=True."""
        from code_indexer.server.mcp import handlers

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_file_service = MagicMock()
            mock_app.file_service = mock_file_service
            mock_file_service.get_file_content.return_value = _make_file_service_response(
                "Full file content\n" * MEDIUM_FILE_LINES
            )
            mock_app.app.state.payload_cache = None

            params = {"repository_alias": "test-repo", "file_path": "test.py"}
            handlers.get_file_content(params, mock_user)

            mock_file_service.get_file_content.assert_called_once()
            call_kwargs = mock_file_service.get_file_content.call_args[1]
            assert call_kwargs.get("skip_truncation") is True, (
                "MCP handler MUST call FileService with skip_truncation=True"
            )

    def test_handler_calls_by_path_with_skip_truncation_true(self, mock_user):
        """Verify MCP handler calls get_file_content_by_path with skip_truncation=True."""
        from code_indexer.server.mcp import handlers

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app,
            patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_func,
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager"
            ) as mock_alias_class,
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir"
            ) as mock_dir,
        ):
            mock_dir.return_value = "/fake/golden/repos"
            mock_registry = MagicMock()
            mock_registry.list_global_repos.return_value = [
                {"alias_name": "test-repo-global", "target_path": "/fake/repo"}
            ]
            mock_registry_func.return_value = mock_registry
            mock_alias = MagicMock()
            mock_alias.read_alias.return_value = "/fake/repo"
            mock_alias_class.return_value = mock_alias

            mock_file_service = MagicMock()
            mock_app.file_service = mock_file_service
            mock_file_service.get_file_content_by_path.return_value = (
                _make_file_service_response("Full file content\n" * MEDIUM_FILE_LINES)
            )
            mock_app.app.state.payload_cache = None

            params = {"repository_alias": "test-repo-global", "file_path": "test.py"}
            handlers.get_file_content(params, mock_user)

            mock_file_service.get_file_content_by_path.assert_called_once()
            call_kwargs = mock_file_service.get_file_content_by_path.call_args[1]
            assert call_kwargs.get("skip_truncation") is True


class TestTruncationHelperIntegration:
    """Test TruncationHelper receives full content and generates cache_handle."""

    def test_truncation_helper_receives_full_content(
        self, mock_user, mock_payload_cache
    ):
        """Verify TruncationHelper receives full (untruncated) content."""
        from code_indexer.server.mcp import handlers

        large_content = "# Line content here\n" * LARGE_FILE_LINES

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_file_service = MagicMock()
            mock_app.file_service = mock_file_service
            mock_file_service.get_file_content.return_value = _make_file_service_response(
                large_content, "large_file.py", LARGE_FILE_LINES
            )
            mock_app.app.state.payload_cache = mock_payload_cache

            with patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc:
                mock_config_svc.return_value.get_config.return_value = (
                    _mock_config_service(LOW_TOKEN_LIMIT)
                )

                params = {"repository_alias": "test-repo", "file_path": "large_file.py"}
                handlers.get_file_content(params, mock_user)

                mock_payload_cache.store.assert_called_once()
                stored_content = mock_payload_cache.store.call_args[0][0]
                assert stored_content == large_content

    def test_cache_handle_returned_for_large_file(
        self, mock_user, mock_payload_cache
    ):
        """Verify cache_handle is returned in response when content is truncated."""
        from code_indexer.server.mcp import handlers

        large_content = "# Line content here\n" * HUGE_FILE_LINES
        mock_payload_cache.store.return_value = "cache-handle-xyz"

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_file_service = MagicMock()
            mock_app.file_service = mock_file_service
            mock_file_service.get_file_content.return_value = _make_file_service_response(
                large_content, "huge_file.py", HUGE_FILE_LINES
            )
            mock_app.app.state.payload_cache = mock_payload_cache

            with patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc:
                mock_config_svc.return_value.get_config.return_value = (
                    _mock_config_service(LOW_TOKEN_LIMIT)
                )

                params = {"repository_alias": "test-repo", "file_path": "huge_file.py"}
                mcp_response = handlers.get_file_content(params, mock_user)

                data = _extract_response_data(mcp_response)
                assert data.get("cache_handle") == "cache-handle-xyz"
                assert data.get("truncated") is True
                assert data.get("has_more") is True


class TestBackwardCompatibility:
    """Test that existing functionality is preserved."""

    def test_small_file_no_truncation(self, mock_user, mock_payload_cache):
        """Verify small files are returned without truncation or cache_handle."""
        from code_indexer.server.mcp import handlers

        small_content = "# Small file\nprint('hello')\n"

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_file_service = MagicMock()
            mock_app.file_service = mock_file_service
            mock_file_service.get_file_content.return_value = _make_file_service_response(
                small_content, "small.py", SMALL_FILE_LINES
            )
            mock_app.app.state.payload_cache = mock_payload_cache

            with patch(
                "code_indexer.server.mcp.handlers.get_config_service"
            ) as mock_config_svc:
                mock_config_svc.return_value.get_config.return_value = (
                    _mock_config_service(HIGH_TOKEN_LIMIT)
                )

                params = {"repository_alias": "test-repo", "file_path": "small.py"}
                mcp_response = handlers.get_file_content(params, mock_user)

                data = _extract_response_data(mcp_response)
                assert data.get("cache_handle") is None
                assert data.get("truncated") is False
                mock_payload_cache.store.assert_not_called()
