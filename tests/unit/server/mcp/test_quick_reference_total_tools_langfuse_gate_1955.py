"""
Bug #1955 item 6: cidx_quick_reference() reported total_tools: 147 even in a
solo deployment where the real tools/list (filter_tools_by_role) returns 145
-- a 2-tool gap exactly matching the two Langfuse-gated tools
(start_trace/end_trace, requires_config: langfuse_enabled in their
frontmatter). quick_reference's own total_tools loop checked only
permission, never requires_config, so it always counted these two tools
regardless of whether Langfuse was actually enabled on this deployment --
the count silently disagreed with what tools/list would really serve.

This test proves the count is DERIVED from the same requires_config gate
filter_tools_by_role/tools/list already applies, not merely "hardcoded"
in the literal sense -- the bug was an incomplete filter, not a magic
number.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import quick_reference
from code_indexer.server.auth.user_manager import User, UserRole

# The two tools whose frontmatter declares requires_config: langfuse_enabled
# (start_trace.md, end_trace.md) -- see src/code_indexer/server/mcp/tool_docs/tracing/.
LANGFUSE_GATED_TOOL_COUNT = 2


def _extract_mcp_data(mcp_response: dict) -> dict:
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])  # type: ignore[no-any-return]
    return {}


@pytest.fixture
def power_user() -> User:
    return User(
        username="test",
        password_hash="hashed_password",
        role=UserRole.POWER_USER,
        created_at=datetime.now(),
    )


def _quick_reference_with_langfuse(power_user: User, *, enabled: bool) -> dict:
    mock_config = MagicMock()
    mock_config.service_display_name = "Neo"
    mock_config.langfuse_config.enabled = enabled
    with patch(
        "code_indexer.server.mcp.handlers.guides.get_config_service"
    ) as mock_get_service:
        mock_service = MagicMock()
        mock_service.get_config.return_value = mock_config
        mock_get_service.return_value = mock_service
        mcp_response = quick_reference({}, power_user)
    return _extract_mcp_data(mcp_response)


def test_quick_reference_total_tools_excludes_langfuse_gated_tools_when_disabled(
    power_user: User,
) -> None:
    enabled_result = _quick_reference_with_langfuse(power_user, enabled=True)
    disabled_result = _quick_reference_with_langfuse(power_user, enabled=False)

    tracing_tools_enabled = {
        t["name"] for t in enabled_result["tools_by_category"].get("tracing", [])
    }
    assert {"start_trace", "end_trace"}.issubset(tracing_tools_enabled), (
        "fixture sanity: with Langfuse enabled, both tracing tools must be counted"
    )

    tracing_tools_disabled = {
        t["name"] for t in disabled_result["tools_by_category"].get("tracing", [])
    }
    assert "start_trace" not in tracing_tools_disabled
    assert "end_trace" not in tracing_tools_disabled

    assert (
        disabled_result["total_tools"]
        == enabled_result["total_tools"] - LANGFUSE_GATED_TOOL_COUNT
    ), (
        f"disabling Langfuse must drop total_tools by exactly the "
        f"{LANGFUSE_GATED_TOOL_COUNT} Langfuse-gated tools (start_trace, end_trace) -- "
        f"got enabled={enabled_result['total_tools']}, disabled={disabled_result['total_tools']}"
    )
