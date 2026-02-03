"""
Unit tests for convert_tool_docs.py conversion script.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC3: Conversion Script Output - Generate 128 .md files with valid frontmatter.
"""

import pytest


@pytest.fixture
def temp_output_dir(tmp_path):
    """Create a temporary output directory."""
    output_dir = tmp_path / "tool_docs"
    output_dir.mkdir()
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
        (output_dir / category).mkdir()
    return output_dir


class TestConvertToolDocs:
    """Tests for the tool documentation conversion script."""

    def test_categorize_tool_assigns_correct_category(self):
        """categorize_tool should assign correct category based on tool name."""
        from tools.convert_tool_docs import categorize_tool

        assert categorize_tool("search_code") == "search"
        assert categorize_tool("regex_search") == "search"
        assert categorize_tool("git_log") == "git"
        assert categorize_tool("git_commit") == "git"
        assert categorize_tool("scip_definition") == "scip"
        assert categorize_tool("create_file") == "files"
        assert categorize_tool("cidx_ssh_key_create") == "ssh"
        assert categorize_tool("first_time_user_guide") == "guides"
        assert categorize_tool("gh_actions_list_runs") == "cicd"
        assert categorize_tool("github_actions_list_runs") == "cicd"
        assert categorize_tool("gitlab_ci_list_pipelines") == "cicd"

    def test_extract_tl_dr_from_description(self):
        """extract_tl_dr should extract TL;DR from description."""
        from tools.convert_tool_docs import extract_tl_dr

        desc = "TL;DR: Search code using indexes. More details here."
        assert extract_tl_dr(desc) == "Search code using indexes."

        desc2 = "No TL;DR prefix in this description."
        assert extract_tl_dr(desc2) == "No TL;DR prefix in this description."

    def test_convert_single_tool_creates_md_file(self, temp_output_dir):
        """convert_tool should create a properly formatted .md file."""
        from tools.convert_tool_docs import convert_tool

        tool_def = {
            "name": "test_tool",
            "description": "TL;DR: Test tool. Full description here.",
            "required_permission": "query_repos",
        }

        convert_tool("test_tool", tool_def, temp_output_dir, "search")

        md_file = temp_output_dir / "search" / "test_tool.md"
        assert md_file.exists()

        content = md_file.read_text()
        assert "name: test_tool" in content
        assert "category: search" in content
        assert "required_permission: query_repos" in content
        assert "tl_dr: Test tool." in content

    def test_convert_all_tools_creates_128_files(self, temp_output_dir):
        """convert_all_tools should create 128 .md files."""
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        stats = convert_all_tools(TOOL_REGISTRY, temp_output_dir)

        assert stats["total"] == 128
        assert stats["converted"] == 128
        assert stats["failed"] == 0

    def test_generated_files_have_valid_frontmatter(self, temp_output_dir):
        """Generated files should have valid YAML frontmatter."""
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        convert_all_tools(TOOL_REGISTRY, temp_output_dir)

        # Should load without errors
        loader = ToolDocLoader(temp_output_dir)
        docs = loader.load_all_docs()
        assert len(docs) == 128
