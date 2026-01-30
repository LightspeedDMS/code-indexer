"""
Unit tests for ToolDocLoader category overview functionality.

TDD tests for:
- CategoryMeta dataclass
- _load_category_meta method
- get_category_overview method

These tests define expected behavior before implementation.
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
def populated_docs_dir(temp_docs_dir):
    """Create a docs directory with sample tools and category metadata."""
    # Add _category.yaml to search
    (temp_docs_dir / "search" / "_category.yaml").write_text(
        "name: search\n"
        "description: Semantic and full-text code search across repositories\n"
    )

    # Add sample tools to search
    (temp_docs_dir / "search" / "search_code.md").write_text(
        "---\nname: search_code\ncategory: search\n"
        "required_permission: query_repos\ntl_dr: Semantic code search.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )
    (temp_docs_dir / "search" / "regex_search.md").write_text(
        "---\nname: regex_search\ncategory: search\n"
        "required_permission: query_repos\ntl_dr: Regex pattern search.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )
    (temp_docs_dir / "search" / "browse_directory.md").write_text(
        "---\nname: browse_directory\ncategory: search\n"
        "required_permission: query_repos\ntl_dr: Browse directory contents.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )
    (temp_docs_dir / "search" / "list_files.md").write_text(
        "---\nname: list_files\ncategory: search\n"
        "required_permission: query_repos\ntl_dr: List files in repository.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )

    # Add _category.yaml to scip
    (temp_docs_dir / "scip" / "_category.yaml").write_text(
        "name: scip\n"
        "description: Code intelligence - find definitions, references, dependencies\n"
    )

    # Add sample tools to scip
    (temp_docs_dir / "scip" / "scip_definition.md").write_text(
        "---\nname: scip_definition\ncategory: scip\n"
        "required_permission: query_repos\ntl_dr: Find symbol definitions.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )
    (temp_docs_dir / "scip" / "scip_references.md").write_text(
        "---\nname: scip_references\ncategory: scip\n"
        "required_permission: query_repos\ntl_dr: Find symbol references.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )

    # Add _category.yaml to git (no tools - should be excluded from overview)
    (temp_docs_dir / "git" / "_category.yaml").write_text(
        "name: git\n"
        "description: Git operations and history exploration\n"
    )

    # Add guides category without _category.yaml (test fallback behavior)
    (temp_docs_dir / "guides" / "quick_ref.md").write_text(
        "---\nname: quick_ref\ncategory: guides\n"
        "required_permission: query_repos\ntl_dr: Quick reference.\n"
        "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
    )

    return temp_docs_dir


class TestCategoryMetaDataclass:
    """Tests for CategoryMeta dataclass."""

    def test_category_meta_exists(self):
        """CategoryMeta dataclass should exist in tool_doc_loader module."""
        from code_indexer.server.mcp.tool_doc_loader import CategoryMeta

        meta = CategoryMeta(name="search", description="Search tools")
        assert meta.name == "search"
        assert meta.description == "Search tools"

    def test_category_meta_fields(self):
        """CategoryMeta should have name and description fields."""
        from code_indexer.server.mcp.tool_doc_loader import CategoryMeta

        meta = CategoryMeta(name="scip", description="Code intelligence tools")
        assert hasattr(meta, "name")
        assert hasattr(meta, "description")


class TestLoadCategoryMeta:
    """Tests for _load_category_meta method."""

    def test_load_category_meta_from_yaml(self, temp_docs_dir):
        """_load_category_meta should parse _category.yaml file."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create _category.yaml
        (temp_docs_dir / "search" / "_category.yaml").write_text(
            "name: search\n"
            "description: Semantic and full-text code search across repositories\n"
        )

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        assert meta is not None
        assert meta.name == "search"
        assert meta.description == "Semantic and full-text code search across repositories"

    def test_load_category_meta_missing_file_returns_none(self, temp_docs_dir):
        """_load_category_meta should return None when _category.yaml is missing."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        assert meta is None

    def test_load_category_meta_uses_directory_name_as_fallback(self, temp_docs_dir):
        """_load_category_meta should use directory name if name field is missing."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create _category.yaml without name field
        (temp_docs_dir / "search" / "_category.yaml").write_text(
            "description: Search tools only\n"
        )

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        assert meta is not None
        assert meta.name == "search"  # Falls back to directory name
        assert meta.description == "Search tools only"

    def test_load_category_meta_empty_description(self, temp_docs_dir):
        """_load_category_meta should handle empty description."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create _category.yaml with only name
        (temp_docs_dir / "search" / "_category.yaml").write_text(
            "name: search\n"
        )

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        assert meta is not None
        assert meta.name == "search"
        assert meta.description == ""

    def test_load_category_meta_malformed_yaml_returns_fallback(self, temp_docs_dir):
        """_load_category_meta should return fallback when YAML is malformed."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create _category.yaml with invalid YAML syntax
        (temp_docs_dir / "search" / "_category.yaml").write_text(
            "name: search\n"
            "description: [invalid yaml here\n"  # Missing closing bracket
            "  - not properly indented\n"
        )

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        # Should return fallback CategoryMeta, not crash
        assert meta is not None
        assert meta.name == "search"  # Falls back to directory name
        assert meta.description == ""  # Empty description on error

    def test_load_category_meta_empty_file(self, temp_docs_dir):
        """_load_category_meta should return fallback for empty _category.yaml."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create empty _category.yaml
        (temp_docs_dir / "search" / "_category.yaml").write_text("")

        loader = ToolDocLoader(temp_docs_dir)
        meta = loader._load_category_meta(temp_docs_dir / "search")

        # Empty file parses to None via yaml.safe_load, handled by "not isinstance(data, dict)"
        assert meta is not None
        assert meta.name == "search"  # Falls back to directory name
        assert meta.description == ""


class TestGetCategoryOverview:
    """Tests for get_category_overview method."""

    def test_get_category_overview_returns_all_categories(self, populated_docs_dir):
        """get_category_overview should return info for all categories with tools."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        # Should include search, scip, guides (have tools)
        # Should NOT include git (has _category.yaml but no tools)
        category_names = [cat["name"] for cat in overview]
        assert "search" in category_names
        assert "scip" in category_names
        assert "guides" in category_names
        assert "git" not in category_names  # No tools

    def test_get_category_overview_includes_description(self, populated_docs_dir):
        """get_category_overview should include category descriptions."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        search_cat = next(cat for cat in overview if cat["name"] == "search")
        assert search_cat["description"] == "Semantic and full-text code search across repositories"

        scip_cat = next(cat for cat in overview if cat["name"] == "scip")
        assert scip_cat["description"] == "Code intelligence - find definitions, references, dependencies"

    def test_get_category_overview_includes_key_tools(self, populated_docs_dir):
        """get_category_overview should include first 3 tools alphabetically."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        search_cat = next(cat for cat in overview if cat["name"] == "search")
        # First 3 alphabetically: browse_directory, list_files, regex_search
        assert len(search_cat["key_tools"]) == 3
        assert search_cat["key_tools"][0] == "browse_directory"
        assert search_cat["key_tools"][1] == "list_files"
        assert search_cat["key_tools"][2] == "regex_search"

    def test_get_category_overview_sorted_alphabetically(self, populated_docs_dir):
        """get_category_overview should return categories sorted alphabetically."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        category_names = [cat["name"] for cat in overview]
        assert category_names == sorted(category_names)

    def test_get_category_overview_excludes_empty_categories(self, populated_docs_dir):
        """get_category_overview should not include categories without tools."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        # git has _category.yaml but no tools, should be excluded
        category_names = [cat["name"] for cat in overview]
        assert "git" not in category_names

        # admin, files, ssh, cicd, repos have no tools in this fixture
        assert "admin" not in category_names
        assert "files" not in category_names

    def test_get_category_overview_includes_tool_count(self, populated_docs_dir):
        """get_category_overview should include tool count per category."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        search_cat = next(cat for cat in overview if cat["name"] == "search")
        assert search_cat["tool_count"] == 4  # search_code, regex_search, browse_directory, list_files

        scip_cat = next(cat for cat in overview if cat["name"] == "scip")
        assert scip_cat["tool_count"] == 2  # scip_definition, scip_references

    def test_get_category_overview_loads_docs_if_not_loaded(self, populated_docs_dir):
        """get_category_overview should call load_all_docs if not already loaded."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        assert loader._loaded is False

        overview = loader.get_category_overview()

        assert loader._loaded is True
        assert len(overview) > 0

    def test_get_category_overview_empty_description_for_missing_meta(self, populated_docs_dir):
        """Categories without _category.yaml should have empty description."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        # guides has no _category.yaml in the fixture
        guides_cat = next(cat for cat in overview if cat["name"] == "guides")
        assert guides_cat["description"] == ""

    def test_get_category_overview_key_tools_less_than_three(self, temp_docs_dir):
        """Categories with fewer than 3 tools should return all available tools."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Add _category.yaml to scip
        (temp_docs_dir / "scip" / "_category.yaml").write_text(
            "name: scip\n"
            "description: Code intelligence\n"
        )

        # Add only 1 tool
        (temp_docs_dir / "scip" / "scip_definition.md").write_text(
            "---\nname: scip_definition\ncategory: scip\n"
            "required_permission: query_repos\ntl_dr: Find definitions.\n"
            "inputSchema:\n  type: object\n  properties: {}\n---\n\nDescription."
        )

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        overview = loader.get_category_overview()

        scip_cat = next(cat for cat in overview if cat["name"] == "scip")
        assert len(scip_cat["key_tools"]) == 1
        assert scip_cat["key_tools"][0] == "scip_definition"


class TestCategoryOverviewReturnStructure:
    """Tests for the return structure of get_category_overview."""

    def test_return_type_is_list(self, populated_docs_dir):
        """get_category_overview should return a list."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        overview = loader.get_category_overview()

        assert isinstance(overview, list)

    def test_each_item_has_required_fields(self, populated_docs_dir):
        """Each category dict should have name, description, key_tools, tool_count."""
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        loader = ToolDocLoader(populated_docs_dir)
        overview = loader.get_category_overview()

        for cat in overview:
            assert "name" in cat
            assert "description" in cat
            assert "key_tools" in cat
            assert "tool_count" in cat
            assert isinstance(cat["name"], str)
            assert isinstance(cat["description"], str)
            assert isinstance(cat["key_tools"], list)
            assert isinstance(cat["tool_count"], int)
