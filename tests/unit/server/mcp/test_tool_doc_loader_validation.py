"""
Unit tests for ToolDocLoader frontmatter validation.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC2: Frontmatter Schema Compliance - Required fields and validation.
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


class TestFrontmatterRequiredFields:
    """Tests for required frontmatter fields validation."""

    def test_missing_required_field_raises_error(self, temp_docs_dir):
        """Missing tl_dr field should raise FrontmatterValidationError."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader, FrontmatterValidationError

        search_dir = temp_docs_dir / "search"
        (search_dir / "missing_field.md").write_text(
            "---\nname: missing_field\ncategory: search\n"
            "required_permission: query_repos\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        with pytest.raises(FrontmatterValidationError) as exc_info:
            loader.load_all_docs()
        assert "tl_dr" in str(exc_info.value)

    def test_empty_permission_is_valid(self, temp_docs_dir):
        """Empty required_permission is valid for public tools."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        guides_dir = temp_docs_dir / "guides"
        (guides_dir / "public_tool.md").write_text(
            "---\nname: public_tool\ncategory: guides\n"
            'required_permission: ""\ntl_dr: Public tool.\n---\n\nDescription.'
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader._cache["public_tool"].required_permission == ""

    def test_optional_quick_reference_defaults_false(self, temp_docs_dir):
        """quick_reference should default to False when not specified."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "no_quick_ref.md").write_text(
            "---\nname: no_quick_ref\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Test.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader._cache["no_quick_ref"].quick_reference is False

    def test_optional_quick_reference_true(self, temp_docs_dir):
        """quick_reference: true should be parsed correctly."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "with_quick_ref.md").write_text(
            "---\nname: with_quick_ref\ncategory: search\n"
            "required_permission: query_repos\ntl_dr: Test.\nquick_reference: true\n---\n\nDesc."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        assert loader._cache["with_quick_ref"].quick_reference is True


class TestFrontmatterFormatErrors:
    """Tests for frontmatter format error handling."""

    def test_invalid_yaml_raises_error(self, temp_docs_dir):
        """Invalid YAML syntax should raise FrontmatterValidationError."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader, FrontmatterValidationError

        search_dir = temp_docs_dir / "search"
        (search_dir / "bad_yaml.md").write_text(
            "---\nname: bad_yaml\ncategory: search\n"
            "required_permission: [invalid\ntl_dr: Test.\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        with pytest.raises(FrontmatterValidationError):
            loader.load_all_docs()

    def test_missing_frontmatter_raises_error(self, temp_docs_dir):
        """File without frontmatter should raise FrontmatterValidationError."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader, FrontmatterValidationError

        search_dir = temp_docs_dir / "search"
        (search_dir / "no_frontmatter.md").write_text("Just markdown without frontmatter.")

        loader = ToolDocLoader(temp_docs_dir)
        with pytest.raises(FrontmatterValidationError):
            loader.load_all_docs()

    def test_nonexistent_directory_raises_error(self, tmp_path):
        """Nonexistent docs directory should raise FileNotFoundError."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        nonexistent = tmp_path / "nonexistent"
        loader = ToolDocLoader(nonexistent)
        with pytest.raises(FileNotFoundError):
            loader.load_all_docs()

    def test_parameters_field_parsed_correctly(self, temp_docs_dir):
        """Optional parameters field should be parsed as dict."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "with_params.md").write_text(
            "---\nname: with_params\ncategory: search\nrequired_permission: query_repos\n"
            "tl_dr: Test.\nparameters:\n  query_text: Search query.\n  limit: Max results.\n"
            "---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        params = loader._cache["with_params"].parameters
        assert params == {"query_text": "Search query.", "limit": "Max results."}
