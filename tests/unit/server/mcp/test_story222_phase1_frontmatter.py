"""
Unit tests for Story #222 Phase 1 TODOs 2-3:
  TODO 2: Add required_permission: query_repos to guide tool docs.
  TODO 3: Remove quick_reference flag from ToolDoc dataclass.

TDD: These tests are written FIRST to define expected behavior.
"""

from pathlib import Path
import tempfile
import yaml


TOOL_DOCS_DIR = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
)


def _get_frontmatter(md_file: Path) -> dict:
    """Parse frontmatter from a tool doc markdown file."""
    content = md_file.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    if len(parts) < 2:
        raise ValueError(f"Missing frontmatter delimiters in {md_file}")
    return yaml.safe_load(parts[1]) or {}


class TestGuideRequiredPermission:
    """Guide tool docs must have required_permission: query_repos, not null."""

    def test_first_time_user_guide_has_query_repos_permission(self):
        """first_time_user_guide required_permission must be query_repos."""
        md_file = TOOL_DOCS_DIR / "guides" / "first_time_user_guide.md"
        fm = _get_frontmatter(md_file)
        assert fm.get("required_permission") == "query_repos", (
            f"Expected 'query_repos', got {fm.get('required_permission')!r}"
        )

    def test_get_tool_categories_has_query_repos_permission(self):
        """get_tool_categories required_permission must be query_repos."""
        md_file = TOOL_DOCS_DIR / "guides" / "get_tool_categories.md"
        fm = _get_frontmatter(md_file)
        assert fm.get("required_permission") == "query_repos", (
            f"Expected 'query_repos', got {fm.get('required_permission')!r}"
        )


class TestQuickReferenceFieldRemoval:
    """quick_reference field must be removed from ToolDoc dataclass and cidx_quick_reference.md."""

    def test_tool_doc_dataclass_has_no_quick_reference_field(self):
        """ToolDoc dataclass must not have quick_reference field."""
        import dataclasses
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        field_names = {f.name for f in dataclasses.fields(ToolDoc)}
        assert "quick_reference" not in field_names, (
            "ToolDoc still has quick_reference field - it must be removed"
        )

    def test_tool_doc_loader_has_no_generate_quick_reference_method(self):
        """ToolDocLoader must not have generate_quick_reference() method."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        assert not hasattr(ToolDocLoader, "generate_quick_reference"), (
            "ToolDocLoader still has generate_quick_reference() - it must be removed"
        )

    def test_cidx_quick_reference_md_has_no_quick_reference_flag(self):
        """cidx_quick_reference.md frontmatter must not have quick_reference field."""
        md_file = TOOL_DOCS_DIR / "guides" / "cidx_quick_reference.md"
        fm = _get_frontmatter(md_file)
        assert "quick_reference" not in fm, (
            "cidx_quick_reference.md still has quick_reference in frontmatter: "
            f"{fm.get('quick_reference')!r}"
        )

    def test_parse_md_file_accepts_file_without_quick_reference(self):
        """Parsing a file without quick_reference must succeed."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "tool_docs"
            search_dir = docs_dir / "search"
            search_dir.mkdir(parents=True)
            (search_dir / "test_tool.md").write_text(
                "---\nname: test_tool\ncategory: search\n"
                "required_permission: query_repos\ntl_dr: Short test.\n"
                "---\n\nDescription.\n"
            )
            loader = ToolDocLoader(docs_dir)
            docs = loader.load_all_docs()
            assert "test_tool" in docs


class TestCategoryOverviewNoQuickReference:
    """get_category_overview must work without quick_reference on ToolDoc."""

    def test_get_category_overview_works_without_quick_reference(self):
        """get_category_overview must not raise AttributeError about quick_reference."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "tool_docs"
            search_dir = docs_dir / "search"
            search_dir.mkdir(parents=True)

            (docs_dir / "search" / "_category.yaml").write_text(
                "name: search\ndescription: Search tools.\n"
            )
            (search_dir / "search_code.md").write_text(
                "---\nname: search_code\ncategory: search\n"
                "required_permission: query_repos\ntl_dr: Search code.\n"
                "inputSchema:\n  type: object\n  properties: {}\n  required: []\n"
                "---\n\nDescription.\n"
            )
            (search_dir / "regex_search.md").write_text(
                "---\nname: regex_search\ncategory: search\n"
                "required_permission: query_repos\ntl_dr: Regex search.\n"
                "inputSchema:\n  type: object\n  properties: {}\n  required: []\n"
                "---\n\nDescription.\n"
            )

            loader = ToolDocLoader(docs_dir)
            loader.load_all_docs()

            overview = loader.get_category_overview()
            assert len(overview) == 1
            assert overview[0]["name"] == "search"
            assert overview[0]["tool_count"] == 2
            assert len(overview[0]["key_tools"]) > 0
