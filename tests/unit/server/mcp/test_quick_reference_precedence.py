"""
Tests for Story #987 AC6:
  cidx_quick_reference handler gains optional 'tool' parameter with precedence logic.

Combinations tested:
  1. tool present -> return full body (category ignored)
  2. tool absent + category present -> existing category-filtered behavior unchanged
  3. both absent -> existing default behavior unchanged
  4. unknown tool -> {success: false, error: "Tool '<name>' not found"}
  5. tool present + category present -> tool wins (category ignored)
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal fixtures and helpers
# ---------------------------------------------------------------------------


def _make_user() -> MagicMock:
    """Create a mock User that has the query_repos permission."""
    user = MagicMock()
    user.has_permission.return_value = True
    user.username = "testuser"
    return user


def _parse_response(mcp_result: dict) -> Any:
    """Extract the inner JSON dict from the MCP content wrapper.

    _mcp_response() returns: {"content": [{"type": "text", "text": "<json>"}]}
    """
    text = mcp_result["content"][0]["text"]
    return json.loads(text)


def _make_docs_dir(tmp_path: Path) -> Path:
    """Create a minimal tool_docs directory with a search tool that has a full body."""
    docs_dir = tmp_path / "tool_docs"
    search_dir = docs_dir / "search"
    search_dir.mkdir(parents=True)
    guides_dir = docs_dir / "guides"
    guides_dir.mkdir(parents=True)

    # A search tool with full body, slim_description, and inputSchema
    (search_dir / "test_search.md").write_text(
        "---\n"
        "name: test_search\n"
        "category: search\n"
        "required_permission: query_repos\n"
        "tl_dr: Short tl_dr for test_search.\n"
        "slim_description: 'Slim one-liner.'\n"
        "inputSchema:\n"
        "  type: object\n"
        "  properties: {}\n"
        "  required: []\n"
        "---\n\n"
        "Full extended body for test_search with all the details.\n",
        encoding="utf-8",
    )
    # A guide with inputSchema so it appears in TOOL_REGISTRY
    (guides_dir / "cidx_quick_reference.md").write_text(
        "---\n"
        "name: cidx_quick_reference\n"
        "category: guides\n"
        "required_permission: query_repos\n"
        "tl_dr: Quick reference guide.\n"
        "inputSchema:\n"
        "  type: object\n"
        "  properties: {}\n"
        "  required: []\n"
        "---\n\n"
        "Full guide body for cidx_quick_reference.\n",
        encoding="utf-8",
    )
    return docs_dir


class TestQuickReferencePrecedence:
    """AC6: 'tool' parameter takes precedence over 'category' in cidx_quick_reference."""

    def _call_handler(self, params: dict, docs_dir: Path) -> Any:
        """Call the quick_reference handler with a patched loader using tmp docs_dir.

        Returns the parsed inner dict from the MCP content wrapper.
        """
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader
        from code_indexer.server.mcp.handlers import guides

        loader = ToolDocLoader(docs_dir)
        loader.load_all_docs()

        user = _make_user()

        mock_config = MagicMock()
        mock_config.service_display_name = "TestServer"

        tool_registry = {
            "test_search": {
                "name": "test_search",
                "description": "Slim one-liner.",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "required_permission": "query_repos",
            },
            "cidx_quick_reference": {
                "name": "cidx_quick_reference",
                "description": "Quick reference guide.",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
                "required_permission": "query_repos",
            },
        }

        mock_app_module = MagicMock()
        mock_app_module.golden_repo_manager = None

        with (
            patch(
                "code_indexer.server.mcp.tool_doc_loader._get_tool_doc_loader",
                return_value=loader,
            ),
            patch(
                "code_indexer.server.mcp.handlers.guides.get_config_service",
                return_value=MagicMock(get_config=lambda: mock_config),
            ),
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module",
                mock_app_module,
            ),
            patch(
                "code_indexer.server.mcp.tools.TOOL_REGISTRY",
                tool_registry,
            ),
        ):
            raw = guides.quick_reference(params, user)
            return _parse_response(raw)

    def test_tool_param_returns_full_body(self, tmp_path):
        """When 'tool' is present, return the tool's full body."""
        docs_dir = _make_docs_dir(tmp_path)
        result = self._call_handler({"tool": "test_search"}, docs_dir)

        assert result["success"] is True
        # The full body must appear under a 'body' key in the response
        assert "body" in result, (
            f"Response must contain 'body' field, got keys: {list(result.keys())}"
        )
        assert "Full extended body for test_search" in result["body"]

    def test_tool_param_ignores_category(self, tmp_path):
        """When 'tool' is present, 'category' is ignored and same body returned."""
        docs_dir = _make_docs_dir(tmp_path)
        result_with_cat = self._call_handler(
            {"tool": "test_search", "category": "guides"}, docs_dir
        )
        result_without_cat = self._call_handler({"tool": "test_search"}, docs_dir)

        assert result_with_cat["success"] is True
        assert result_without_cat["success"] is True
        # Both should return the same full body
        assert result_with_cat["body"] == result_without_cat["body"]

    def test_unknown_tool_returns_error(self, tmp_path):
        """When 'tool' names an unknown tool, return success=false with error message."""
        docs_dir = _make_docs_dir(tmp_path)
        result = self._call_handler({"tool": "definitely_not_a_tool"}, docs_dir)

        assert result["success"] is False
        assert "error" in result
        assert "definitely_not_a_tool" in result["error"]

    def test_category_only_returns_filtered_list(self, tmp_path):
        """When 'tool' absent and 'category' present, existing behavior is unchanged."""
        docs_dir = _make_docs_dir(tmp_path)
        result = self._call_handler({"category": "search"}, docs_dir)

        assert result["success"] is True
        # Existing behavior: returns tools_by_category dict
        assert "tools_by_category" in result
        assert "total_tools" in result

    def test_both_absent_returns_all_tools(self, tmp_path):
        """When both 'tool' and 'category' are absent, existing default behavior unchanged."""
        docs_dir = _make_docs_dir(tmp_path)
        result = self._call_handler({}, docs_dir)

        assert result["success"] is True
        assert "tools_by_category" in result
        assert "total_tools" in result
        # category_filter key absent or null when not filtering
        assert result.get("category_filter") is None
