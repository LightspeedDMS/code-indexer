"""
Unit tests for Story #987 - Loader supports slim_description frontmatter.

AC1: ToolDoc dataclass gains slim_description field
AC2: Frontmatter parser reads slim_description gracefully
AC3: Tool registry uses slim_description for description field
AC4: get_extended_description method with module-level cache
AC7: memory category added to VALID_CATEGORIES
AC9: Tests cover slim parsing (present/absent/whitespace), description fallback,
     get_extended_description (hit/miss/cache), VALID_CATEGORIES includes memory
"""

import dataclasses
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        "memory",
    ]:
        (docs_dir / category).mkdir()
    return docs_dir


def _make_tool_md(
    docs_dir: Path,
    category: str,
    name: str,
    slim_description: str = "",
    body: str = "Full body content of the tool documentation.",
    include_input_schema: bool = False,
) -> Path:
    """Helper to create a tool markdown file in a category directory."""
    slim_line = ""
    if slim_description:
        slim_line = f"slim_description: {slim_description!r}\n"

    schema_lines = ""
    if include_input_schema:
        schema_lines = (
            "inputSchema:\n  type: object\n  properties: {}\n  required: []\n"
        )

    content = (
        "---\n"
        f"name: {name}\n"
        f"category: {category}\n"
        "required_permission: query_repos\n"
        f"tl_dr: Short description of {name}.\n"
        f"{slim_line}"
        f"{schema_lines}"
        "---\n\n"
        f"{body}"
    )
    md_file = docs_dir / category / f"{name}.md"
    md_file.write_text(content, encoding="utf-8")
    return md_file


# ---------------------------------------------------------------------------
# AC1: ToolDoc dataclass gains slim_description field
# ---------------------------------------------------------------------------


class TestToolDocSlimDescriptionField:
    """AC1: ToolDoc dataclass must have slim_description: Optional[str] = None."""

    def test_tool_doc_has_slim_description_field(self):
        """ToolDoc dataclass must have slim_description field."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        field_names = {f.name for f in dataclasses.fields(ToolDoc)}
        assert "slim_description" in field_names, (
            "ToolDoc must have a slim_description field"
        )

    def test_slim_description_default_is_none(self):
        """slim_description must default to None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        doc = ToolDoc(
            name="test",
            category="search",
            required_permission="query_repos",
            tl_dr="Test tool.",
            description="Body text.",
        )
        assert doc.slim_description is None

    def test_slim_description_accepts_string_value(self):
        """slim_description must accept a string value without breaking constructor."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        doc = ToolDoc(
            name="test",
            category="search",
            required_permission="query_repos",
            tl_dr="Test tool.",
            description="Body text.",
            slim_description="Slim text for list view.",
        )
        assert doc.slim_description == "Slim text for list view."

    def test_slim_description_is_optional(self):
        """slim_description field must be Optional[str]."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        field_map = {f.name: f for f in dataclasses.fields(ToolDoc)}
        slim_field = field_map["slim_description"]
        # Default must be None (not MISSING)
        assert slim_field.default is None


# ---------------------------------------------------------------------------
# AC2: Frontmatter parser reads slim_description gracefully
# ---------------------------------------------------------------------------


class TestSlimDescriptionFrontmatterParsing:
    """AC2: _parse_md_file() reads slim_description from frontmatter."""

    def test_parses_slim_description_when_present(self, temp_docs_dir):
        """slim_description in frontmatter is loaded into ToolDoc."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(
            temp_docs_dir,
            "search",
            "slim_tool",
            slim_description="Concise one-liner for list view.",
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        doc = loader._cache["slim_tool"]
        assert doc.slim_description == "Concise one-liner for list view."

    def test_slim_description_none_when_absent_from_frontmatter(self, temp_docs_dir):
        """slim_description is None when not present in frontmatter (no exception)."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(temp_docs_dir, "search", "no_slim_tool")
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        doc = loader._cache["no_slim_tool"]
        assert doc.slim_description is None

    def test_whitespace_only_slim_description_treated_as_none(self, temp_docs_dir):
        """Whitespace-only slim_description is normalized to None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Manually write file with whitespace-only slim_description
        md_file = temp_docs_dir / "search" / "ws_slim.md"
        md_file.write_text(
            "---\n"
            "name: ws_slim\n"
            "category: search\n"
            "required_permission: query_repos\n"
            "tl_dr: Whitespace slim test.\n"
            "slim_description: '   '\n"
            "---\n\nBody content.\n",
            encoding="utf-8",
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        doc = loader._cache["ws_slim"]
        assert doc.slim_description is None

    def test_empty_string_slim_description_treated_as_none(self, temp_docs_dir):
        """Empty string slim_description is normalized to None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        md_file = temp_docs_dir / "search" / "empty_slim.md"
        md_file.write_text(
            "---\n"
            "name: empty_slim\n"
            "category: search\n"
            "required_permission: query_repos\n"
            "tl_dr: Empty slim test.\n"
            "slim_description: ''\n"
            "---\n\nBody content.\n",
            encoding="utf-8",
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        doc = loader._cache["empty_slim"]
        assert doc.slim_description is None


# ---------------------------------------------------------------------------
# AC3: Tool registry uses slim_description for description field
# ---------------------------------------------------------------------------


class TestBuildToolRegistrySlimPreference:
    """AC3: build_tool_registry() prefers slim_description over body excerpt."""

    def test_registry_uses_slim_description_when_present(self, temp_docs_dir):
        """build_tool_registry() uses slim_description as description when present."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(
            temp_docs_dir,
            "search",
            "slim_reg_tool",
            slim_description="Slim registry description.",
            body="Full body that should NOT appear as description.",
            include_input_schema=True,
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        registry = loader.build_tool_registry()
        assert "slim_reg_tool" in registry
        assert registry["slim_reg_tool"]["description"] == "Slim registry description."

    def test_registry_falls_back_to_body_when_slim_absent(self, temp_docs_dir):
        """build_tool_registry() uses body[:500] when slim_description is None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        long_body = "X" * 600 + " remainder"
        _make_tool_md(
            temp_docs_dir,
            "search",
            "no_slim_reg",
            body=long_body,
            include_input_schema=True,
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        registry = loader.build_tool_registry()
        assert "no_slim_reg" in registry
        desc = registry["no_slim_reg"]["description"]
        # Fallback: body[:500] - should be 500 chars long
        assert len(desc) == 500
        assert desc == "X" * 500

    def test_registry_uses_full_body_when_shorter_than_500(self, temp_docs_dir):
        """build_tool_registry() uses entire body when shorter than 500 chars."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        short_body = "Short body content."
        _make_tool_md(
            temp_docs_dir,
            "search",
            "short_body_reg",
            body=short_body,
            include_input_schema=True,
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        registry = loader.build_tool_registry()
        assert "short_body_reg" in registry
        assert registry["short_body_reg"]["description"] == short_body


# ---------------------------------------------------------------------------
# AC4: get_extended_description method with module-level cache
# ---------------------------------------------------------------------------


class TestGetExtendedDescription:
    """AC4: ToolDocLoader.get_extended_description() returns full body with caching."""

    def test_get_extended_description_returns_body_for_known_tool(self, temp_docs_dir):
        """get_extended_description() returns full body for a known tool."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(
            temp_docs_dir,
            "search",
            "ext_tool",
            body="Full extended body with all the details.",
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        result = loader.get_extended_description("ext_tool")
        assert result == "Full extended body with all the details."

    def test_get_extended_description_returns_none_for_unknown_tool(
        self, temp_docs_dir
    ):
        """get_extended_description() returns None for unknown tool names."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        result = loader.get_extended_description("definitely_not_a_tool")
        assert result is None

    def test_get_extended_description_is_idempotent(self, temp_docs_dir):
        """get_extended_description() returns the same result on repeated calls."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(
            temp_docs_dir, "search", "idem_tool", body="Idempotent body content."
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        result1 = loader.get_extended_description("idem_tool")
        result2 = loader.get_extended_description("idem_tool")
        assert result1 == result2 == "Idempotent body content."

    def test_get_extended_description_cached_same_object(self, temp_docs_dir):
        """get_extended_description() returns the same string object on repeated calls (cache hit)."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(
            temp_docs_dir, "search", "cache_tool", body="Cached body content."
        )
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        result1 = loader.get_extended_description("cache_tool")
        result2 = loader.get_extended_description("cache_tool")
        # Same object identity (cached, not recomputed)
        assert result1 is result2


# ---------------------------------------------------------------------------
# AC7: memory category added to VALID_CATEGORIES
# ---------------------------------------------------------------------------


class TestMemoryCategoryInValidCategories:
    """AC7: VALID_CATEGORIES must include 'memory'."""

    def test_memory_in_valid_categories(self):
        """'memory' must be in VALID_CATEGORIES."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        assert "memory" in ToolDocLoader.VALID_CATEGORIES, (
            "'memory' is not in ToolDocLoader.VALID_CATEGORIES"
        )

    def test_memory_category_tool_loads_successfully(self, temp_docs_dir):
        """A tool in the memory category loads without errors."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        _make_tool_md(temp_docs_dir, "memory", "create_memory", body="Create a memory.")
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        assert "create_memory" in loader._cache
        assert loader._cache["create_memory"].category == "memory"
