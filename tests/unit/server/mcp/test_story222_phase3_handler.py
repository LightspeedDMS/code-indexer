"""
Unit tests for Story #222 Phase 3 TODOs 7-12: Handler refactoring.

  TODO 7: Replace hardcoded category dict with frontmatter-driven lookup.
  TODO 8: Replace TL;DR body-parsing with frontmatter tl_dr.
  TODO 9: Build compact grouped catalog (tools_by_category).
  TODO 10: Compress _build_langfuse_section to 4 compact fields.
  TODO 11: Compress _build_dependency_map_section to single compact string.
  TODO 12: Compress discovery section to single compact string.

TDD: These tests are written FIRST to define expected behavior.
"""

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

import pytest


def _extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON data from MCP-compliant content array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


@pytest.fixture
def test_user():
    """Create a test user with full permissions."""
    from code_indexer.server.auth.user_manager import User, UserRole
    return User(
        username="test",
        password_hash="hashed_password",
        role=UserRole.POWER_USER,
        created_at=datetime.now(),
    )


@pytest.fixture
def mock_config_no_langfuse():
    """Mock config with no Langfuse, standard display name."""
    config = MagicMock()
    config.service_display_name = "Neo"
    config.langfuse_config = None
    return config


def _call_quick_reference(params, test_user, mock_config, golden_repo_manager=None):
    """Helper: call quick_reference with mocked config and app_module."""
    from code_indexer.server.mcp.handlers import quick_reference

    with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_svc, \
         patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
        mock_service = MagicMock()
        mock_service.get_config.return_value = mock_config
        mock_svc.return_value = mock_service
        mock_app.golden_repo_manager = golden_repo_manager

        return quick_reference(params, test_user)


class TestGroupedCatalogFormat:
    """TODO 9: Response must use tools_by_category grouped dict, not flat tools array."""

    def test_response_has_tools_by_category(self, test_user, mock_config_no_langfuse):
        """Response must have 'tools_by_category' grouped dict."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)

        assert result["success"] is True
        assert "tools_by_category" in result, (
            "Response must have 'tools_by_category' grouped dict"
        )
        assert isinstance(result["tools_by_category"], dict)

    def test_each_category_has_list_of_tools(self, test_user, mock_config_no_langfuse):
        """Each category in tools_by_category must be a list of tool dicts."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category, tools in tools_by_category.items():
            assert isinstance(tools, list), f"Category {category!r} must be a list"
            for tool in tools:
                assert isinstance(tool, dict), (
                    f"Tool entry in {category!r} must be a dict, got {type(tool)}"
                )

    def test_each_tool_entry_has_name_and_tl_dr(self, test_user, mock_config_no_langfuse):
        """Each tool entry must have exactly 'name' and 'tl_dr' keys."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category, tools in tools_by_category.items():
            for tool in tools:
                assert "name" in tool, f"Tool in {category!r} missing 'name': {tool}"
                assert "tl_dr" in tool, f"Tool in {category!r} missing 'tl_dr': {tool}"

    def test_tool_entries_have_no_extra_fields(self, test_user, mock_config_no_langfuse):
        """Tool entries must not have required_permission, category, or summary."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category, tools in tools_by_category.items():
            for tool in tools:
                assert "required_permission" not in tool, (
                    f"Tool entry must not have 'required_permission': {tool}"
                )
                assert "summary" not in tool, (
                    f"Tool entry must not have old 'summary' field: {tool}"
                )

    def test_category_filter_works_with_grouped_format(self, test_user, mock_config_no_langfuse):
        """category filter must still work and return only matching category."""
        mcp_response = _call_quick_reference(
            {"category": "search"}, test_user, mock_config_no_langfuse
        )
        result = _extract_mcp_data(mcp_response)

        assert result["success"] is True
        tools_by_category = result["tools_by_category"]
        assert "search" in tools_by_category

        for category in tools_by_category.keys():
            assert category == "search", (
                f"With category='search' filter, only 'search' should appear, got {category!r}"
            )


class TestFrontmatterDrivenCategories:
    """TODO 7: Handler must use ToolDoc.category from frontmatter, not hardcoded dict."""

    def test_categories_match_valid_frontmatter_categories(self, test_user, mock_config_no_langfuse):
        """All categories in response must be valid frontmatter categories."""
        valid_categories = {
            "search", "git", "scip", "files", "admin",
            "repos", "ssh", "guides", "cicd", "tracing"
        }
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category in tools_by_category.keys():
            assert category in valid_categories, (
                f"Category {category!r} is not a valid frontmatter category. "
                "Handler must use ToolDoc.category from frontmatter."
            )

    def test_no_hardcoded_category_names_in_response(self, test_user, mock_config_no_langfuse):
        """Old hardcoded category names like 'git_exploration' must not appear."""
        old_hardcoded_categories = {
            "git_exploration", "git_operations", "repo_management",
            "golden_repos", "system", "user_management", "ssh_keys", "meta"
        }
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category in tools_by_category.keys():
            assert category not in old_hardcoded_categories, (
                f"Old hardcoded category {category!r} found in response. "
                "Handler must use frontmatter categories, not hardcoded dict."
            )


class TestFrontmatterDrivenTlDr:
    """TODO 8: tl_dr for each tool must come from frontmatter, not body text parsing."""

    def test_tl_dr_values_are_short(self, test_user, mock_config_no_langfuse):
        """tl_dr values from frontmatter must be <= 80 chars (after Phase 1 trimming)."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        violations = []
        for category, tools in tools_by_category.items():
            for tool in tools:
                tl_dr = tool["tl_dr"]
                if len(tl_dr) > 80:
                    violations.append(
                        f"{tool['name']} ({category}): {len(tl_dr)} chars"
                    )

        assert violations == [], (
            "tl_dr values in response must be <= 80 chars (from frontmatter):\n"
            + "\n".join(violations)
        )

    def test_tl_dr_does_not_start_with_tldr_prefix(self, test_user, mock_config_no_langfuse):
        """tl_dr from frontmatter must not start with 'TL;DR:' prefix."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        tools_by_category = result["tools_by_category"]

        for category, tools in tools_by_category.items():
            for tool in tools:
                tl_dr = tool["tl_dr"]
                assert not tl_dr.startswith("TL;DR:"), (
                    f"{tool['name']}: tl_dr starts with 'TL;DR:' prefix - "
                    "must use frontmatter value, not body text"
                )


class TestCompressedLangfuseSection:
    """TODO 10: _build_langfuse_section must return compact 4-field dict."""

    def _make_langfuse_config(self, project_count: int = 1):
        """Create a mock config with Langfuse pull enabled."""
        from dataclasses import dataclass, field as dc_field
        from typing import List, Optional

        @dataclass
        class LangfusePullProject:
            public_key: str = ""
            secret_key: str = ""

        @dataclass
        class LangfuseConfig:
            enabled: bool = True
            pull_enabled: bool = True
            pull_projects: List = dc_field(default_factory=list)

        @dataclass
        class ServerConfig:
            langfuse_config: Optional[LangfuseConfig] = None
            service_display_name: str = "Neo"

        projects = [LangfusePullProject(f"pk{i}", f"sk{i}") for i in range(project_count)]
        return ServerConfig(
            langfuse_config=LangfuseConfig(enabled=True, pull_enabled=True, pull_projects=projects)
        )

    def test_section_has_exactly_4_fields(self):
        """Compressed section must have exactly 4 fields."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock

        config = self._make_langfuse_config()
        manager = Mock()
        manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, manager)
        assert result is not None

        expected_fields = {
            "description", "search_pattern",
            "available_repositories", "configured_projects_count"
        }
        assert set(result.keys()) == expected_fields, (
            f"Section must have exactly {expected_fields}, got {set(result.keys())}"
        )

    def test_description_field_is_string(self):
        """description field must be a string."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock

        config = self._make_langfuse_config()
        manager = Mock()
        manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, manager)
        assert isinstance(result["description"], str)
        assert len(result["description"]) > 0

    def test_search_pattern_mentions_search_code(self):
        """search_pattern must reference search_code function."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock

        config = self._make_langfuse_config()
        manager = Mock()
        manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, manager)
        assert "search_code" in result["search_pattern"], (
            "search_pattern must reference search_code function"
        )
        assert "langfuse" in result["search_pattern"].lower(), (
            "search_pattern must reference langfuse repos"
        )

    def test_available_repositories_filters_langfuse(self):
        """available_repositories must only include langfuse_* repos."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock

        config = self._make_langfuse_config()
        manager = Mock()
        manager.list_golden_repos.return_value = [
            {"alias": "langfuse_proj_user1"},
            {"alias": "other_repo"},
            {"alias": "langfuse_proj_user2"},
        ]

        result = _build_langfuse_section(config, manager)
        repos = result["available_repositories"]
        assert "langfuse_proj_user1" in repos
        assert "langfuse_proj_user2" in repos
        assert "other_repo" not in repos

    def test_configured_projects_count_correct(self):
        """configured_projects_count must match the number of configured projects."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock

        config = self._make_langfuse_config(project_count=3)
        manager = Mock()
        manager.list_golden_repos.return_value = []

        result = _build_langfuse_section(config, manager)
        assert result["configured_projects_count"] == 3

    def test_section_returns_none_when_disabled(self):
        """Must return None when pull_enabled is False."""
        from code_indexer.server.mcp.handlers import _build_langfuse_section
        from unittest.mock import Mock
        from dataclasses import dataclass, field as dc_field
        from typing import List, Optional

        @dataclass
        class LangfuseConfig:
            pull_enabled: bool = False
            pull_projects: List = dc_field(default_factory=list)

        @dataclass
        class ServerConfig:
            langfuse_config: Optional[LangfuseConfig] = None

        config = ServerConfig(langfuse_config=LangfuseConfig(pull_enabled=False))
        result = _build_langfuse_section(config, Mock())
        assert result is None


class TestCompressedDependencyMapSection:
    """TODO 11: _build_dependency_map_section must return compact single-line string."""

    def test_section_is_compact_string_under_200_chars(self):
        """Section must be a compact string <= 200 chars."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()
            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")
            (dep_map_dir / "domain2.md").write_text("# Domain 2\n")

            result = _build_dependency_map_section(cidx_meta_path)

        assert result != ""
        assert isinstance(result, str)
        assert len(result) <= 200, (
            f"Compact section must be <= 200 chars, got {len(result)}: {result!r}"
        )

    def test_section_mentions_dependency_map(self):
        """Compact string must mention dependency-map."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()
            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "d.md").write_text("# D\n")

            result = _build_dependency_map_section(cidx_meta_path)

        assert "dependency-map" in result, "Must mention dependency-map"

    def test_section_mentions_index_md(self):
        """Compact string must mention _index.md."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()
            (dep_map_dir / "_index.md").write_text("# Index\n")
            (dep_map_dir / "d.md").write_text("# D\n")

            result = _build_dependency_map_section(cidx_meta_path)

        assert "_index.md" in result, "Must mention _index.md"

    def test_section_includes_domain_count(self):
        """Compact string must include the domain count."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()
            (dep_map_dir / "_index.md").write_text("# Index\n")
            for i in range(4):
                (dep_map_dir / f"domain{i}.md").write_text(f"# {i}\n")

            result = _build_dependency_map_section(cidx_meta_path)

        assert "4" in result, f"Must include domain count '4', got: {result!r}"

    def test_section_empty_when_directory_missing(self):
        """Must return empty string when dependency-map dir is missing."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            result = _build_dependency_map_section(cidx_meta_path)

        assert result == ""

    def test_section_empty_when_index_missing(self):
        """Must return empty string when _index.md is missing."""
        from code_indexer.server.mcp.handlers import _build_dependency_map_section

        with TemporaryDirectory() as tmp:
            cidx_meta_path = Path(tmp)
            dep_map_dir = cidx_meta_path / "dependency-map"
            dep_map_dir.mkdir()
            (dep_map_dir / "domain1.md").write_text("# Domain 1\n")
            # No _index.md

            result = _build_dependency_map_section(cidx_meta_path)

        assert result == ""


class TestCompressedDiscoverySection:
    """TODO 12: discovery section must be a compact string, not a verbose dict."""

    def test_discovery_is_string(self, test_user, mock_config_no_langfuse):
        """discovery value must be a string."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)

        assert "discovery" in result, "Response must have 'discovery' section"
        assert isinstance(result["discovery"], str), (
            f"discovery must be a string, got {type(result['discovery']).__name__}"
        )

    def test_discovery_mentions_cidx_meta_global(self, test_user, mock_config_no_langfuse):
        """Compact discovery string must mention cidx-meta-global."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        assert "cidx-meta-global" in result["discovery"]

    def test_discovery_mentions_search_code(self, test_user, mock_config_no_langfuse):
        """Compact discovery string must mention search_code."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        assert "search_code" in result["discovery"]

    def test_discovery_mentions_repo_alias_pattern(self, test_user, mock_config_no_langfuse):
        """Compact discovery string must explain file_path -> repo_alias mapping."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        discovery = result["discovery"]
        assert ".md" in discovery or "-global" in discovery, (
            "Must explain the file_path -> repo_alias pattern"
        )

    def test_discovery_is_under_400_chars(self, test_user, mock_config_no_langfuse):
        """Compact discovery string must be <= 400 chars."""
        mcp_response = _call_quick_reference({}, test_user, mock_config_no_langfuse)
        result = _extract_mcp_data(mcp_response)
        discovery = result["discovery"]
        assert len(discovery) <= 400, (
            f"Compact discovery must be <= 400 chars, got {len(discovery)}: {discovery!r}"
        )
