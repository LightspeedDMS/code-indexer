"""
Unit tests for ToolDocLoader inputSchema support and registry building.

This module tests the ability to build TOOL_REGISTRY dynamically from markdown files
that contain inputSchema in their YAML frontmatter.
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


class TestInputSchemaLoading:
    """Tests for inputSchema extraction from frontmatter."""

    def test_load_tool_with_inputschema(self, temp_docs_dir):
        """Verify inputSchema is parsed from frontmatter."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "test_tool.md").write_text(
            """---
name: test_tool
category: search
required_permission: query_repos
tl_dr: Test tool for searching.
inputSchema:
  type: object
  properties:
    query_text:
      type: string
      description: The search query
    limit:
      type: integer
      default: 10
  required:
    - query_text
---

Full description of the test tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        tool_doc = loader._cache["test_tool"]
        assert tool_doc.inputSchema is not None
        assert tool_doc.inputSchema["type"] == "object"
        assert "query_text" in tool_doc.inputSchema["properties"]
        assert tool_doc.inputSchema["properties"]["query_text"]["type"] == "string"
        assert tool_doc.inputSchema["required"] == ["query_text"]

    def test_tool_without_inputschema_has_none(self, temp_docs_dir):
        """Tools without inputSchema field should have inputSchema=None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        guides_dir = temp_docs_dir / "guides"
        (guides_dir / "guide_doc.md").write_text(
            """---
name: guide_doc
category: guides
required_permission: query_repos
tl_dr: A guide document.
---

This is a guide, not a tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        tool_doc = loader._cache["guide_doc"]
        assert tool_doc.inputSchema is None

    def test_complex_inputschema_with_oneof(self, temp_docs_dir):
        """Verify complex schemas with oneOf work correctly."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "complex_tool.md").write_text(
            """---
name: complex_tool
category: search
required_permission: query_repos
tl_dr: Complex tool with oneOf schema.
inputSchema:
  type: object
  properties:
    repository_alias:
      oneOf:
        - type: string
        - type: array
          items:
            type: string
      description: Repository alias or array of aliases
    search_mode:
      type: string
      enum:
        - semantic
        - fts
        - hybrid
      default: semantic
  required:
    - repository_alias
---

Complex tool description.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        tool_doc = loader._cache["complex_tool"]
        assert tool_doc.inputSchema is not None
        prop = tool_doc.inputSchema["properties"]["repository_alias"]
        assert "oneOf" in prop
        assert len(prop["oneOf"]) == 2
        assert prop["oneOf"][0]["type"] == "string"
        assert prop["oneOf"][1]["type"] == "array"

        search_mode = tool_doc.inputSchema["properties"]["search_mode"]
        assert search_mode["enum"] == ["semantic", "fts", "hybrid"]

    def test_inputschema_with_nested_objects(self, temp_docs_dir):
        """Verify nested object schemas are preserved."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        admin_dir = temp_docs_dir / "admin"
        (admin_dir / "nested_tool.md").write_text(
            """---
name: nested_tool
category: admin
required_permission: manage_users
tl_dr: Tool with nested object schema.
inputSchema:
  type: object
  properties:
    config:
      type: object
      properties:
        enabled:
          type: boolean
        options:
          type: object
          properties:
            timeout:
              type: integer
            retries:
              type: integer
  required:
    - config
---

Nested schema description.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        tool_doc = loader._cache["nested_tool"]
        assert tool_doc.inputSchema is not None
        config = tool_doc.inputSchema["properties"]["config"]
        assert config["type"] == "object"
        assert "options" in config["properties"]
        assert (
            config["properties"]["options"]["properties"]["timeout"]["type"]
            == "integer"
        )


class TestBuildToolRegistry:
    """Tests for build_tool_registry() method."""

    def test_build_tool_registry_basic(self, temp_docs_dir):
        """Verify build_tool_registry returns correct format."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_tool.md").write_text(
            """---
name: search_tool
category: search
required_permission: query_repos
tl_dr: Search for code.
inputSchema:
  type: object
  properties:
    query:
      type: string
  required:
    - query
---

Search tool description with details.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        registry = loader.build_tool_registry()

        assert "search_tool" in registry
        assert registry["search_tool"]["name"] == "search_tool"
        assert registry["search_tool"]["required_permission"] == "query_repos"
        assert registry["search_tool"]["inputSchema"]["type"] == "object"
        # Description should be the markdown body, not tl_dr
        assert (
            "Search tool description with details"
            in registry["search_tool"]["description"]
        )

    def test_build_tool_registry_skips_tools_without_schema(self, temp_docs_dir):
        """Verify tools without inputSchema are excluded from registry."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "with_schema.md").write_text(
            """---
name: with_schema
category: search
required_permission: query_repos
tl_dr: Has schema.
inputSchema:
  type: object
  properties: {}
---

Has schema.
"""
        )

        guides_dir = temp_docs_dir / "guides"
        (guides_dir / "no_schema.md").write_text(
            """---
name: no_schema
category: guides
required_permission: query_repos
tl_dr: No schema guide.
---

Guide without schema.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        registry = loader.build_tool_registry()

        assert "with_schema" in registry
        assert "no_schema" not in registry

    def test_build_tool_registry_auto_loads(self, temp_docs_dir):
        """build_tool_registry should auto-load if not already loaded."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "auto_load.md").write_text(
            """---
name: auto_load
category: search
required_permission: query_repos
tl_dr: Auto loaded.
inputSchema:
  type: object
  properties: {}
---

Auto loaded tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        assert loader._loaded is False

        registry = loader.build_tool_registry()

        assert loader._loaded is True
        assert "auto_load" in registry

    def test_build_tool_registry_multiple_tools(self, temp_docs_dir):
        """Verify multiple tools are included in registry."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        for i in range(3):
            (search_dir / f"tool_{i}.md").write_text(
                f"""---
name: tool_{i}
category: search
required_permission: query_repos
tl_dr: Tool {i}.
inputSchema:
  type: object
  properties:
    param_{i}:
      type: string
---

Tool {i} description.
"""
            )

        loader = ToolDocLoader(temp_docs_dir)
        registry = loader.build_tool_registry()

        assert len(registry) == 3
        for i in range(3):
            assert f"tool_{i}" in registry
            assert f"param_{i}" in registry[f"tool_{i}"]["inputSchema"]["properties"]


class TestGetToolsByCategory:
    """Tests for get_tools_by_category() method."""

    def test_get_tools_by_category_groups_correctly(self, temp_docs_dir):
        """Verify tools are grouped by category."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_tool.md").write_text(
            """---
name: search_tool
category: search
required_permission: query_repos
tl_dr: Search for code.
inputSchema:
  type: object
  properties: {}
---

Search tool.
"""
        )

        git_dir = temp_docs_dir / "git"
        (git_dir / "git_tool.md").write_text(
            """---
name: git_tool
category: git
required_permission: query_repos
tl_dr: Git operations.
inputSchema:
  type: object
  properties: {}
---

Git tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        categories = loader.get_tools_by_category()

        assert "search" in categories
        assert "git" in categories
        assert len(categories["search"]) == 1
        assert len(categories["git"]) == 1
        assert categories["search"][0]["name"] == "search_tool"
        assert categories["git"][0]["name"] == "git_tool"

    def test_get_tools_by_category_includes_tl_dr(self, temp_docs_dir):
        """Verify tl_dr is included in category output."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_tool.md").write_text(
            """---
name: search_tool
category: search
required_permission: query_repos
tl_dr: Search code semantically.
inputSchema:
  type: object
  properties: {}
---

Search tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        categories = loader.get_tools_by_category()

        assert categories["search"][0]["tl_dr"] == "Search code semantically."

    def test_get_tools_by_category_skips_guides(self, temp_docs_dir):
        """Verify guides without inputSchema are excluded."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "search_tool.md").write_text(
            """---
name: search_tool
category: search
required_permission: query_repos
tl_dr: Search tool.
inputSchema:
  type: object
  properties: {}
---

Search tool.
"""
        )

        guides_dir = temp_docs_dir / "guides"
        (guides_dir / "guide_doc.md").write_text(
            """---
name: guide_doc
category: guides
required_permission: query_repos
tl_dr: Guide document.
---

Guide without schema.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        categories = loader.get_tools_by_category()

        assert "search" in categories
        # guides category should either not exist or be empty
        if "guides" in categories:
            assert len(categories["guides"]) == 0 or all(
                t.get("inputSchema") is not None for t in categories.get("guides", [])
            )

    def test_get_tools_by_category_auto_loads(self, temp_docs_dir):
        """get_tools_by_category should auto-load if not loaded."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        (search_dir / "tool.md").write_text(
            """---
name: tool
category: search
required_permission: query_repos
tl_dr: Tool.
inputSchema:
  type: object
  properties: {}
---

Tool.
"""
        )

        loader = ToolDocLoader(temp_docs_dir)
        assert loader._loaded is False

        categories = loader.get_tools_by_category()

        assert loader._loaded is True
        assert "search" in categories

    def test_get_tools_by_category_multiple_per_category(self, temp_docs_dir):
        """Verify multiple tools per category are listed."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        search_dir = temp_docs_dir / "search"
        for name in ["search_a", "search_b", "search_c"]:
            (search_dir / f"{name}.md").write_text(
                f"""---
name: {name}
category: search
required_permission: query_repos
tl_dr: {name} description.
inputSchema:
  type: object
  properties: {{}}
---

{name} tool.
"""
            )

        loader = ToolDocLoader(temp_docs_dir)
        categories = loader.get_tools_by_category()

        assert len(categories["search"]) == 3
        names = [t["name"] for t in categories["search"]]
        assert "search_a" in names
        assert "search_b" in names
        assert "search_c" in names


class TestToolDocDataclassUpdate:
    """Tests for ToolDoc dataclass with inputSchema field."""

    def test_tooldoc_has_inputschema_field(self, temp_docs_dir):
        """Verify ToolDoc dataclass includes inputSchema field."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        doc = ToolDoc(
            name="test",
            category="search",
            required_permission="query_repos",
            tl_dr="Test",
            description="Test description",
            inputSchema={"type": "object", "properties": {}},
        )

        assert doc.inputSchema is not None
        assert doc.inputSchema["type"] == "object"

    def test_tooldoc_inputschema_defaults_to_none(self):
        """Verify inputSchema defaults to None."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDoc

        doc = ToolDoc(
            name="test",
            category="search",
            required_permission="query_repos",
            tl_dr="Test",
            description="Test description",
        )

        assert doc.inputSchema is None
