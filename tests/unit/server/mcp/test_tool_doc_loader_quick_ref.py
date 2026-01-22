"""
Unit tests for ToolDocLoader quick reference generation.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC6: Quick Reference Auto-Generation.
"""

import pytest


@pytest.fixture
def temp_docs_dir(tmp_path):
    """Create a temporary tool_docs directory with category subdirectories."""
    docs_dir = tmp_path / "tool_docs"
    docs_dir.mkdir()
    for category in ["search", "git", "scip", "files", "admin", "repos", "ssh", "guides", "cicd"]:
        (docs_dir / category).mkdir()
    return docs_dir


class TestQuickReferenceGeneration:
    """Tests for quick reference auto-generation (AC6)."""

    def test_collects_tools_with_quick_reference_true(self, temp_docs_dir):
        """Tools with quick_reference: true should be collected."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_code.md").write_text(
            "---\nname: search_code\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Search code using indexes.\nquick_reference: true\n---\n\nDescription."
        )
        (search_dir / "hidden_tool.md").write_text(
            "---\nname: hidden_tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Not in quick ref.\nquick_reference: false\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        quick_ref = loader.generate_quick_reference()

        assert "search_code" in quick_ref
        assert "hidden_tool" not in quick_ref

    def test_groups_tools_by_category(self, temp_docs_dir):
        """Quick reference should group tools by category."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_code.md").write_text(
            "---\nname: search_code\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Search code.\nquick_reference: true\n---\n\nDescription."
        )
        git_dir = temp_docs_dir / "git"
        (git_dir / "git_log.md").write_text(
            "---\nname: git_log\ncategory: git\nrequired_permission: query_repos\n"
            "tl_dr: Browse commits.\nquick_reference: true\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        quick_ref = loader.generate_quick_reference()

        assert "search" in quick_ref.lower()
        assert "git" in quick_ref.lower()

    def test_uses_tl_dr_for_descriptions(self, temp_docs_dir):
        """Quick reference should use tl_dr, not full description."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_code.md").write_text(
            "---\nname: search_code\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Search code using pre-built indexes.\nquick_reference: true\n---\n\n"
            "Long description that should not appear in quick reference."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        quick_ref = loader.generate_quick_reference()

        assert "Search code using pre-built indexes" in quick_ref
        assert "Long description that should not appear" not in quick_ref

    def test_empty_when_no_quick_reference_tools(self, temp_docs_dir):
        """Quick reference should handle case with no flagged tools."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "tool.md").write_text(
            "---\nname: tool\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Tool.\nquick_reference: false\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        quick_ref = loader.generate_quick_reference()

        # Should return something meaningful even if empty
        assert quick_ref is not None
