"""
Tests for SCIP tool schema definitions.

Story #741: Add repository_alias to MCP SCIP Tool Schemas

Validates that all 7 SCIP tools have repository_alias parameter exposed
in their inputSchema with correct type definition.
"""

import pytest

from code_indexer.server.mcp.tools import TOOL_REGISTRY


# All 7 SCIP tools that need repository_alias parameter
SCIP_TOOLS = [
    "scip_definition",
    "scip_references",
    "scip_dependencies",
    "scip_dependents",
    "scip_impact",
    "scip_callchain",
    "scip_context",
]


class TestSCIPToolSchemasRepositoryAlias:
    """Test that all SCIP tools have repository_alias in their schemas."""

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_scip_tool_has_repository_alias_parameter(self, tool_name: str):
        """AC1: repository_alias parameter added to all 7 SCIP tools."""
        assert tool_name in TOOL_REGISTRY, f"Tool {tool_name} not in registry"

        schema = TOOL_REGISTRY[tool_name]
        assert "inputSchema" in schema, f"Tool {tool_name} missing inputSchema"

        properties = schema["inputSchema"].get("properties", {})
        assert (
            "repository_alias" in properties
        ), f"Tool {tool_name} missing repository_alias in inputSchema.properties"

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_accepts_string_type(self, tool_name: str):
        """AC2: repository_alias accepts string type for single repo filter."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Should accept string type
        type_def = repo_alias_schema.get("type")
        if isinstance(type_def, list):
            assert (
                "string" in type_def
            ), f"Tool {tool_name}: repository_alias should accept string type"
        elif isinstance(type_def, str):
            assert (
                type_def == "string"
            ), f"Tool {tool_name}: repository_alias should be string type"
        else:
            # Could be using oneOf pattern
            one_of = repo_alias_schema.get("oneOf", [])
            types = [opt.get("type") for opt in one_of]
            assert (
                "string" in types
            ), f"Tool {tool_name}: repository_alias should accept string type"

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_accepts_null_type(self, tool_name: str):
        """AC2/AC5: repository_alias accepts null (for all repos default behavior)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Should accept null type
        type_def = repo_alias_schema.get("type")
        if isinstance(type_def, list):
            assert (
                "null" in type_def
            ), f"Tool {tool_name}: repository_alias should accept null type"
        else:
            # Could be using oneOf pattern
            one_of = repo_alias_schema.get("oneOf", [])
            types = [opt.get("type") for opt in one_of]
            # Check if null is in types or if default is None
            has_null = "null" in types or repo_alias_schema.get("default") is None
            assert (
                has_null
            ), f"Tool {tool_name}: repository_alias should accept null or have null default"

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_has_null_default(self, tool_name: str):
        """AC5: Default behavior preserved (null = all repos)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Default should be None/null for all repos behavior
        assert (
            repo_alias_schema.get("default") is None
        ), f"Tool {tool_name}: repository_alias default should be null/None"

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_has_description(self, tool_name: str):
        """AC6: Description documents repository_alias parameter usage."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        assert (
            "description" in repo_alias_schema
        ), f"Tool {tool_name}: repository_alias should have description"

        description = repo_alias_schema["description"]
        assert (
            len(description) > 10
        ), f"Tool {tool_name}: repository_alias description too short"
        # Description should mention filtering/repo
        assert (
            "repo" in description.lower()
        ), f"Tool {tool_name}: description should mention repository"

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_not_required(self, tool_name: str):
        """AC5: repository_alias is optional (not in required list)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        required = schema.get("required", [])

        assert (
            "repository_alias" not in required
        ), f"Tool {tool_name}: repository_alias should be optional, not required"


class TestSCIPToolSchemasExistence:
    """Verify all expected SCIP tools exist in registry."""

    def test_all_scip_tools_registered(self):
        """All 7 SCIP tools exist in TOOL_REGISTRY."""
        for tool_name in SCIP_TOOLS:
            assert (
                tool_name in TOOL_REGISTRY
            ), f"SCIP tool {tool_name} not found in TOOL_REGISTRY"

    def test_scip_tools_count(self):
        """Exactly 7 SCIP tools expected."""
        assert len(SCIP_TOOLS) == 7, "Expected exactly 7 SCIP tools"


class TestSCIPToolSchemaBackwardCompatibility:
    """Ensure schema changes don't break existing functionality."""

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_existing_required_fields_preserved(self, tool_name: str):
        """Existing required fields are not affected by schema change."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        required = schema.get("required", [])

        # Each tool has its original required fields
        if tool_name == "scip_callchain":
            # scip_callchain requires from_symbol and to_symbol
            assert "from_symbol" in required
            assert "to_symbol" in required
        else:
            # Other SCIP tools require symbol
            assert "symbol" in required

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_existing_project_filter_preserved(self, tool_name: str):
        """Existing project filter parameter is preserved."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        properties = schema["properties"]

        # All SCIP tools should have project filter (existing functionality)
        assert (
            "project" in properties
        ), f"Tool {tool_name}: existing project filter should be preserved"
