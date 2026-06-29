"""
Integration tests for file content truncation with line-offset pagination.

Story #33: File Content Returns Cache Handle on Truncation
Bug #1080: cache_handle retired; pagination now uses line-offset (next_offset).

Tests AC3 intent: large content is truncated at a line boundary and the full
content is recoverable by successive get_file_content calls with increasing
offset values.  cache_handle is always None after Bug #1080.
"""

from datetime import datetime
import json
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


def _collect_all_content_via_offset(
    handlers, params: dict, mock_user, initial_data: dict
) -> str:
    """Collect all pages of content using line-offset pagination (Bug #1080 contract).

    Bug #1080 retired cache_handle.  Full content is retrieved by successive
    get_file_content calls with offset=next_offset until has_more is False.
    """
    all_chunks = [initial_data.get("file_content", [{}])[0].get("text", "")]
    current_data = initial_data

    while current_data.get("has_more", False):
        meta = current_data.get("metadata", {})
        next_offset = meta.get("next_offset")
        if next_offset is None:
            break

        next_params = dict(params)
        next_params["offset"] = next_offset

        response = handlers.get_file_content(next_params, mock_user)
        current_data = _extract_response_data(response)
        if not current_data.get("success"):
            break
        chunk = current_data.get("file_content", [{}])
        all_chunks.append(chunk[0].get("text", "") if chunk else "")

    return "".join(all_chunks)


def _make_multiline_content(num_lines: int, chars_per_line: int) -> tuple:
    """Build multi-line content and return (content, total_lines).

    Each line is `chars_per_line` x-characters followed by newline.
    total_lines matches what the real FileListingService counts (\\n-based).
    """
    lines = ["x" * chars_per_line + "\n" for _ in range(num_lines)]
    content = "".join(lines)
    # total_lines = number of \\n characters (matching FileListingService semantics)
    total_lines = content.count("\n")
    return content, total_lines


@pytest.mark.slow
class TestCacheRetrievalIntegration:
    """Test complete flow: truncate file -> retrieve full content via offset pagination (AC3)."""

    @pytest.fixture
    def setup_content_limits(self):
        """Set up ContentLimitsConfig: 50 token limit, 4 chars/token => 200 chars max."""
        from code_indexer.server.utils.config_manager import ContentLimitsConfig

        return ContentLimitsConfig(
            chars_per_token=4,
            file_content_max_tokens=50,  # 200 chars max per page
        )

    def test_truncated_content_can_be_retrieved_via_offset_pagination(
        self, mock_user, setup_content_limits
    ):
        """AC3: Full content is recoverable after truncation via offset pagination.

        Bug #1080: cache_handle retired; full content is retrieved by successive
        get_file_content calls with next_offset.  This test proves:
        - Multi-line content exceeding the token budget is truncated (truncated=True)
        - has_more=True and next_offset is set for the next page
        - cache_handle is always None (retired)
        - Successive offset calls reconstruct the full original content
        """
        from unittest.mock import MagicMock, patch
        from code_indexer.server.mcp import handlers

        content_limits = setup_content_limits

        # 10 lines of 50 chars each = 510 chars total, 10 total_lines.
        # max_chars = 50 tokens * 4 chars = 200 chars.
        # _read_chunk budget: 3 lines fit (153 chars), line 4 exceeds => truncated.
        large_content, total_lines = _make_multiline_content(
            num_lines=10, chars_per_line=50
        )
        assert total_lines == 10

        mock_app = MagicMock()
        mock_service = MagicMock()

        def fake_get_file_content(**kwargs):
            offset = kwargs.get("offset") or 1
            # Return FULL content from the requested offset line onward.
            # The handler's _read_chunk will do the actual line-budget slicing.
            lines = large_content.split("\n")
            # offset is 1-indexed
            slice_from = offset - 1
            sliced = "\n".join(lines[slice_from:])
            if lines[-1] == "":
                # preserve trailing newline behaviour
                sliced_lines = large_content.split("\n")
                sliced = "\n".join(sliced_lines[slice_from:])
            return {
                "content": sliced,
                "metadata": {
                    "size": len(large_content),
                    "path": "large_file.py",
                    "total_lines": total_lines,
                    "offset": offset,
                },
            }

        mock_service.get_file_content.side_effect = fake_get_file_content
        mock_app.file_service = mock_service
        mock_app.app.state.payload_cache = None  # not used after Bug #1080

        mock_config_service = MagicMock()
        mock_config_service.get_config.return_value.content_limits_config = (
            content_limits
        )

        params = {"repository_alias": "test-repo", "file_path": "large_file.py"}

        with (
            patch("code_indexer.server.mcp.handlers.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.get_config_service",
                return_value=mock_config_service,
            ),
            patch(
                "code_indexer.server.mcp.handlers.files.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            # Step 1: First page — must be truncated.
            file_response = handlers.get_file_content(params, mock_user)
            file_data = _extract_response_data(file_response)

            assert file_data.get("success") is True, (
                f"Expected success, got: {file_data}"
            )
            assert file_data.get("truncated") is True, (
                f"Expected truncated=True for 510-char / 10-line content with 200-char budget; "
                f"got truncated={file_data.get('truncated')}, metadata={file_data.get('metadata')}"
            )
            assert file_data.get("has_more") is True
            meta = file_data.get("metadata", {})
            assert meta.get("next_offset") is not None, (
                "next_offset must be set when truncated"
            )

            # Bug #1080: cache_handle is always None (retired).
            assert file_data.get("cache_handle") is None
            assert meta.get("cache_handle") is None

            # Step 2: Collect all pages via offset pagination.
            all_content = _collect_all_content_via_offset(
                handlers, params, mock_user, file_data
            )
            assert all_content == large_content, (
                f"Full content mismatch: expected {len(large_content)} chars, "
                f"got {len(all_content)} chars"
            )

    def test_non_truncated_content_has_no_cache_handle(
        self, mock_user, setup_content_limits
    ):
        """AC4: Non-truncated content has cache_handle=None, truncated=False, has_more=False.

        Bug #1080: cache_handle is always None.  Small content fitting within
        the token budget must return the full content in a single response.
        """
        from unittest.mock import MagicMock, patch
        from code_indexer.server.mcp import handlers

        content_limits = setup_content_limits
        small_content = "def hello(): pass"  # 17 chars, well within 200-char budget
        total_lines = small_content.count("\n") + (
            1 if not small_content.endswith("\n") else 0
        )

        mock_app = MagicMock()
        mock_service = MagicMock()
        mock_service.get_file_content.return_value = {
            "content": small_content,
            "metadata": {
                "size": len(small_content),
                "path": "small_file.py",
                "total_lines": total_lines,
                "offset": 1,
            },
        }
        mock_app.file_service = mock_service
        mock_app.app.state.payload_cache = None

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
            patch(
                "code_indexer.server.mcp.handlers.files.get_config_service",
                return_value=mock_config_service,
            ),
        ):
            response = handlers.get_file_content(
                {"repository_alias": "test-repo", "file_path": "small_file.py"},
                mock_user,
            )
            data = _extract_response_data(response)

            assert data.get("success") is True
            assert data.get("truncated") is False
            assert data.get("has_more") is False
            # cache_handle is always None after Bug #1080.
            assert data.get("cache_handle") is None
            assert data.get("metadata", {}).get("cache_handle") is None
            assert data.get("metadata", {}).get("next_offset") is None

            content_blocks = data.get("file_content", [])
            assert len(content_blocks) > 0
            assert content_blocks[0].get("text") == small_content
