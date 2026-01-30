"""
Unit tests for verify_tool_docs.py verification script.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC4: CI Gate Validation - Verify all 128 tools have valid documentation.
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


@pytest.fixture
def valid_md_content():
    """Return valid markdown content with proper frontmatter."""
    return (
        "---\n"
        "name: test_tool\n"
        "category: search\n"
        "required_permission: query_repos\n"
        "tl_dr: Test tool.\n"
        "---\n\n"
        "Full description here."
    )


class TestVerifyToolDocs:
    """Tests for the tool documentation verification script."""

    def test_verify_file_count_matches_registry(self, temp_docs_dir, valid_md_content):
        """verify_docs should fail if file count doesn't match registry."""
        from tools.verify_tool_docs import verify_file_count

        # Create only 1 file
        search_dir = temp_docs_dir / "search"
        (search_dir / "test_tool.md").write_text(valid_md_content)

        # Should fail - registry has 128 tools
        mock_registry = {f"tool_{i}": {} for i in range(128)}
        result = verify_file_count(temp_docs_dir, mock_registry)

        assert result["success"] is False
        assert "expected 128" in result["message"].lower()

    def test_verify_file_count_passes_when_matching(self, temp_docs_dir, valid_md_content):
        """verify_docs should pass when file count matches registry."""
        from tools.verify_tool_docs import verify_file_count

        # Create 3 files to match a mock registry of 3 tools
        search_dir = temp_docs_dir / "search"
        for i in range(3):
            content = valid_md_content.replace("test_tool", f"tool_{i}")
            (search_dir / f"tool_{i}.md").write_text(content)

        mock_registry = {f"tool_{i}": {} for i in range(3)}
        result = verify_file_count(temp_docs_dir, mock_registry)

        assert result["success"] is True

    def test_verify_frontmatter_valid(self, temp_docs_dir, valid_md_content):
        """verify_frontmatter should pass for valid frontmatter."""
        from tools.verify_tool_docs import verify_frontmatter

        search_dir = temp_docs_dir / "search"
        (search_dir / "test_tool.md").write_text(valid_md_content)

        result = verify_frontmatter(temp_docs_dir)

        assert result["success"] is True
        assert result["valid_count"] == 1

    def test_verify_frontmatter_invalid(self, temp_docs_dir):
        """verify_frontmatter should fail for invalid frontmatter."""
        from tools.verify_tool_docs import verify_frontmatter

        search_dir = temp_docs_dir / "search"
        # Missing required tl_dr field
        (search_dir / "bad_tool.md").write_text(
            "---\nname: bad_tool\ncategory: search\nrequired_permission: query_repos\n---\n\nDesc."
        )

        result = verify_frontmatter(temp_docs_dir)

        assert result["success"] is False
        assert "bad_tool" in str(result["errors"])

    def test_verify_registry_coverage(self, temp_docs_dir, valid_md_content):
        """verify_registry_coverage should find missing tools."""
        from tools.verify_tool_docs import verify_registry_coverage

        search_dir = temp_docs_dir / "search"
        (search_dir / "existing_tool.md").write_text(
            valid_md_content.replace("test_tool", "existing_tool")
        )

        mock_registry = {"existing_tool": {}, "missing_tool": {}}
        result = verify_registry_coverage(temp_docs_dir, mock_registry)

        assert result["success"] is False
        assert "missing_tool" in result["missing"]

    def test_verify_all_passes_when_valid(self, temp_docs_dir):
        """verify_all should pass when all checks pass."""
        from tools.verify_tool_docs import verify_all
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        # Generate actual docs from registry
        convert_all_tools(TOOL_REGISTRY, temp_docs_dir)

        result = verify_all(temp_docs_dir, TOOL_REGISTRY)

        assert result["success"] is True
        assert result["file_count"]["success"] is True
        assert result["frontmatter"]["success"] is True
        assert result["coverage"]["success"] is True

    def test_main_returns_exit_code_zero_on_success(self, temp_docs_dir, monkeypatch):
        """main() should return 0 when verification passes."""
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY

        # Generate actual docs
        convert_all_tools(TOOL_REGISTRY, temp_docs_dir)

        # Monkeypatch the docs dir
        import tools.verify_tool_docs as verify_module
        monkeypatch.setattr(verify_module, "DEFAULT_DOCS_DIR", temp_docs_dir)

        exit_code = verify_module.main([])
        assert exit_code == 0

    def test_main_returns_exit_code_one_on_failure(self, temp_docs_dir, monkeypatch):
        """main() should return 1 when verification fails."""
        import tools.verify_tool_docs as verify_module
        monkeypatch.setattr(verify_module, "DEFAULT_DOCS_DIR", temp_docs_dir)

        # Empty docs dir - should fail
        exit_code = verify_module.main([])
        assert exit_code == 1
