"""
Tests for wiki_article_analytics MCP tool - Schema and Registration (Story #293).

Tests cover:
- AC1: Tool doc file exists with correct YAML front matter and inputSchema
- AC6: HANDLER_REGISTRY contains entry with required query_repos permission
"""

from typing import Any, Dict

import yaml
from pathlib import Path


# Derive tool doc path relative to this test file (portable across environments)
_TESTS_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
TOOL_DOC_PATH = (
    _TESTS_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "search"
    / "wiki_article_analytics.md"
)


def _load_tool_doc_meta() -> Dict[str, Any]:
    """Load and parse YAML front matter from the tool doc file."""
    content = TOOL_DOC_PATH.read_text()
    parts = content.split("---", 2)
    return yaml.safe_load(parts[1])


# ============================================================================
# AC1: Tool doc file exists with correct YAML front matter
# ============================================================================


class TestToolDocFile:
    """AC1: Tool doc file exists with correct YAML schema."""

    def test_tool_doc_file_exists(self):
        """AC1: wiki_article_analytics.md tool doc file must exist."""
        assert TOOL_DOC_PATH.exists(), f"Tool doc file not found at {TOOL_DOC_PATH}"

    def test_tool_doc_has_yaml_frontmatter(self):
        """AC1: Tool doc must have valid YAML front matter."""
        content = TOOL_DOC_PATH.read_text()
        assert content.startswith("---"), "Tool doc must start with YAML front matter"
        parts = content.split("---", 2)
        assert len(parts) >= 3, "Tool doc must have closing --- for front matter"

    def test_tool_doc_name_field(self):
        """AC1: front matter must have name=wiki_article_analytics."""
        meta = _load_tool_doc_meta()
        assert meta["name"] == "wiki_article_analytics"

    def test_tool_doc_category_field(self):
        """AC1: front matter must have category=search."""
        meta = _load_tool_doc_meta()
        assert meta["category"] == "search"

    def test_tool_doc_required_permission(self):
        """AC6: front matter must have required_permission=query_repos."""
        meta = _load_tool_doc_meta()
        assert meta["required_permission"] == "query_repos"

    def test_tool_doc_tl_dr(self):
        """AC1: front matter must have a non-empty tl_dr field."""
        meta = _load_tool_doc_meta()
        assert "tl_dr" in meta
        assert len(str(meta["tl_dr"])) > 0

    def test_tool_doc_input_schema_has_repo_alias_required(self):
        """AC1: inputSchema must list repo_alias as required parameter."""
        meta = _load_tool_doc_meta()
        assert "inputSchema" in meta
        schema = meta["inputSchema"]
        assert "required" in schema
        assert "repo_alias" in schema["required"]

    def test_tool_doc_input_schema_repo_alias_is_string(self):
        """AC1: repo_alias property must be type string."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        assert "repo_alias" in props
        assert props["repo_alias"]["type"] == "string"

    def test_tool_doc_input_schema_has_sort_by_enum(self):
        """AC1: inputSchema must have sort_by with enum [most_viewed, least_viewed]."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        assert "sort_by" in props
        sort_by = props["sort_by"]
        assert "enum" in sort_by
        assert "most_viewed" in sort_by["enum"]
        assert "least_viewed" in sort_by["enum"]

    def test_tool_doc_sort_by_default_is_most_viewed(self):
        """AC1: sort_by must default to most_viewed."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        sort_by = props["sort_by"]
        assert sort_by.get("default") == "most_viewed"

    def test_tool_doc_input_schema_has_limit_integer(self):
        """AC1: inputSchema must have limit with integer type."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        assert "limit" in props
        limit = props["limit"]
        assert limit["type"] == "integer"

    def test_tool_doc_limit_default_is_20(self):
        """AC1: limit must default to 20."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        limit = props["limit"]
        assert limit["default"] == 20

    def test_tool_doc_limit_minimum_is_1(self):
        """AC1: limit must have minimum=1."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        limit = props["limit"]
        assert limit.get("minimum") == 1

    def test_tool_doc_input_schema_has_search_query(self):
        """AC1: inputSchema must have optional search_query field."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        assert "search_query" in props
        # search_query is optional - must NOT be in required list
        required = meta["inputSchema"].get("required", [])
        assert "search_query" not in required

    def test_tool_doc_input_schema_has_search_mode_enum(self):
        """AC1: inputSchema must have search_mode with enum [semantic, fts]."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        assert "search_mode" in props
        search_mode = props["search_mode"]
        assert "enum" in search_mode
        assert "semantic" in search_mode["enum"]
        assert "fts" in search_mode["enum"]

    def test_tool_doc_search_mode_default_is_semantic(self):
        """AC1: search_mode must default to semantic."""
        meta = _load_tool_doc_meta()
        props = meta["inputSchema"]["properties"]
        search_mode = props["search_mode"]
        assert search_mode.get("default") == "semantic"


# ============================================================================
# AC6: HANDLER_REGISTRY contains wiki_article_analytics
# ============================================================================


class TestHandlerRegistry:
    """AC6: HANDLER_REGISTRY must contain wiki_article_analytics entry."""

    def test_handler_registry_contains_wiki_article_analytics(self):
        """AC6: HANDLER_REGISTRY must have wiki_article_analytics key."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        assert (
            "wiki_article_analytics" in HANDLER_REGISTRY
        ), "HANDLER_REGISTRY must contain 'wiki_article_analytics' entry"

    def test_handler_registry_entry_is_callable(self):
        """AC6: The registered handler must be callable."""
        from code_indexer.server.mcp.handlers import HANDLER_REGISTRY

        handler = HANDLER_REGISTRY.get("wiki_article_analytics")
        assert callable(handler), "wiki_article_analytics handler must be callable"

    def test_handler_function_exists_in_module(self):
        """AC6: handle_wiki_article_analytics function must exist in handlers module."""
        from code_indexer.server.mcp import handlers

        assert hasattr(
            handlers, "handle_wiki_article_analytics"
        ), "handlers module must have handle_wiki_article_analytics function"
