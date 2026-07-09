"""
Unit tests for Bug #1016: get_file_content MCP response inner payload key rename.

The application-level payload inside the MCP CallToolResult uses `file_content`
(not `content`) to eliminate the naming collision with the protocol-level
`CallToolResult.content` array.

RED phase: these tests fail until files.py is updated to use `file_content`.
"""

import json
from datetime import datetime
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    return User(
        username="testuser",
        role=UserRole.NORMAL_USER,
        password_hash="dummy_hash",
        created_at=datetime.now(),
    )


def _parse_payload(mcp_response: dict) -> Dict[str, Any]:
    """Extract the application-level JSON payload from the MCP protocol wrapper."""
    # Protocol layer: mcp_response["content"][0]["text"] holds JSON string
    result: Dict[str, Any] = json.loads(mcp_response["content"][0]["text"])
    return result


class TestFileContentKeyRename:
    """Bug #1016: inner payload must use file_content key, not content."""

    @pytest.fixture
    def mock_file_service(self):
        with patch("code_indexer.server.mcp.handlers._utils.app_module") as mock_app:
            mock_service = MagicMock()
            mock_app.file_service = mock_service
            mock_app.app.state.payload_cache = None
            yield mock_service, mock_app

    def test_success_response_uses_file_content_key(self, mock_user, mock_file_service):
        """Success payload must expose file_content, not content."""
        from code_indexer.server.mcp import handlers

        mock_service, _ = mock_file_service
        mock_service.get_file_content.return_value = {
            "content": "def hello(): pass",
            "metadata": {"size": 17, "language": "python", "path": "hello.py"},
        }

        mcp_response = handlers.get_file_content(
            {"repository_alias": "test-repo", "file_path": "hello.py"}, mock_user
        )

        payload = _parse_payload(mcp_response)
        assert payload["success"] is True
        assert "file_content" in payload, (
            "application payload must use 'file_content' key (Bug #1016)"
        )
        assert "content" not in payload, (
            "application payload must NOT use 'content' key (naming collision with MCP protocol)"
        )

    def test_success_response_file_content_is_list_of_blocks(
        self, mock_user, mock_file_service
    ):
        """file_content must be a list of MCP-style text blocks."""
        from code_indexer.server.mcp import handlers

        mock_service, _ = mock_file_service
        mock_service.get_file_content.return_value = {
            "content": "def hello(): pass",
            "metadata": {"size": 17, "language": "python", "path": "hello.py"},
        }

        mcp_response = handlers.get_file_content(
            {"repository_alias": "test-repo", "file_path": "hello.py"}, mock_user
        )

        payload = _parse_payload(mcp_response)
        assert isinstance(payload["file_content"], list)
        assert len(payload["file_content"]) > 0
        assert payload["file_content"][0]["type"] == "text"
        assert payload["file_content"][0]["text"] == "def hello(): pass"

    def test_error_response_uses_file_content_key(self, mock_user, mock_file_service):
        """Error payload must also expose file_content (empty list), not content."""
        from code_indexer.server.mcp import handlers

        mock_service, _ = mock_file_service
        mock_service.get_file_content.side_effect = RuntimeError("disk error")

        mcp_response = handlers.get_file_content(
            {"repository_alias": "test-repo", "file_path": "missing.py"}, mock_user
        )

        payload = _parse_payload(mcp_response)
        assert payload["success"] is False
        assert "file_content" in payload, (
            "error payload must use 'file_content' key (Bug #1016)"
        )
        assert "content" not in payload, "error payload must NOT use 'content' key"
        assert payload["file_content"] == []

    def test_invalid_offset_error_response_uses_file_content_key(
        self, mock_user, mock_file_service
    ):
        """Offset validation error must also use file_content key."""
        from code_indexer.server.mcp import handlers

        payload = _parse_payload(
            handlers.get_file_content(
                {
                    "repository_alias": "test-repo",
                    "file_path": "f.py",
                    "offset": 0,
                },
                mock_user,
            )
        )

        assert "file_content" in payload
        assert "content" not in payload

    def test_invalid_limit_error_response_uses_file_content_key(
        self, mock_user, mock_file_service
    ):
        """Limit validation error must also use file_content key."""
        from code_indexer.server.mcp import handlers

        payload = _parse_payload(
            handlers.get_file_content(
                {
                    "repository_alias": "test-repo",
                    "file_path": "f.py",
                    "limit": 0,
                },
                mock_user,
            )
        )

        assert "file_content" in payload
        assert "content" not in payload
