"""
Unit tests for ToolDocLoader - MCP Tool Documentation Loader.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC5: Loader Runtime Behavior - Caching and error handling.
"""

import pytest


@pytest.fixture
def temp_docs_dir(tmp_path):
    """Create a temporary tool_docs directory with category subdirectories."""
    docs_dir = tmp_path / "tool_docs"
    docs_dir.mkdir()
    for category in [
        "search",
        "git",
        "scip",
        "files",
        "admin",
        "repos",
        "ssh",
        "guides",
        "cicd",
    ]:
        (docs_dir / category).mkdir()
    return docs_dir


class TestToolDocLoaderBasicLoading:
    """Basic loading and caching behavior."""

    def test_load_all_docs_caches_results(self, temp_docs_dir):
        """load_all_docs() should cache parsed content."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "test_tool.md").write_text(
            "---\nname: test_tool\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Test tool.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        docs = loader.load_all_docs()
        assert loader._loaded is True
        assert "test_tool" in docs

        docs2 = loader.load_all_docs()
        assert docs2 is docs  # Same object returned (cached)

    def test_get_description_returns_cached_content(self, temp_docs_dir):
        """get_description() should return cached markdown body."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "my_tool.md").write_text(
            "---\nname: my_tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: My tool.\n---\n\nThis is the description body.\nMultiple lines."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        description = loader.get_description("my_tool")

        assert "This is the description body." in description
        assert "Multiple lines." in description

    def test_get_description_raises_for_missing_tool(self, temp_docs_dir):
        """get_description() should raise ToolDocNotFoundError for missing tool."""
        from code_indexer.server.mcp.tool_doc_loader import (
            ToolDocLoader,
            ToolDocNotFoundError,
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader._loaded = True

        with pytest.raises(ToolDocNotFoundError) as exc_info:
            loader.get_description("nonexistent_tool")
        assert "nonexistent_tool" in str(exc_info.value)

    def test_empty_docs_directory_results_in_empty_cache(self, temp_docs_dir):
        """Empty docs directory should result in empty cache."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(temp_docs_dir)
        docs = loader.load_all_docs()

        assert docs == {}
        assert loader._loaded is True


class TestToolDocLoaderPermissionAndParams:
    """Permission and parameter retrieval."""

    def test_get_permission_returns_required_permission(self, temp_docs_dir):
        """get_permission() returns required_permission from frontmatter."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        admin_dir = temp_docs_dir / "admin"
        (admin_dir / "admin_tool.md").write_text(
            "---\nname: admin_tool\ncategory: admin\n"
            "required_permission: manage_golden_repos\ntl_dr: Admin tool.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader.get_permission("admin_tool") == "manage_golden_repos"

    def test_get_param_description_returns_parameter_doc(self, temp_docs_dir):
        """get_param_description() returns specific parameter documentation."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "param_tool.md").write_text(
            "---\nname: param_tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Tool with params.\nparameters:\n  query_text: The search query.\n"
            "  limit: Max results.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        assert (
            loader.get_param_description("param_tool", "query_text")
            == "The search query."
        )
        assert loader.get_param_description("param_tool", "limit") == "Max results."

    def test_get_param_description_returns_none_for_missing(self, temp_docs_dir):
        """get_param_description() returns None for nonexistent parameter."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "no_params.md").write_text(
            "---\nname: no_params\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: No params.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader.get_param_description("no_params", "nonexistent") is None

    def test_validate_against_registry_finds_missing(self, temp_docs_dir):
        """validate_against_registry() checks all TOOL_REGISTRY tools have docs."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_code.md").write_text(
            "---\nname: search_code\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Search code.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        missing = loader.validate_against_registry(TOOL_REGISTRY)
        assert len(missing) == len(TOOL_REGISTRY) - 1
        assert "search_code" not in missing
