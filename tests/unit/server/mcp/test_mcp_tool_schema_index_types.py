"""
Unit tests for MCP tool schema index types.

Verifies that MCP tools use the new individual index types:
- semantic (separate from FTS)
- fts (separate from semantic)
- temporal
- scip

NOT the old combined: semantic_fts

Story #2: Fix Add Index functionality - CRITICAL-3
"""

import pytest
from code_indexer.server.mcp.tools import TOOL_REGISTRY


class TestAddGoldenRepoIndexSchema:
    """Tests for add_golden_repo_index MCP tool schema."""

    def test_schema_exists_in_registry(self):
        """Test that add_golden_repo_index tool is registered."""
        assert "add_golden_repo_index" in TOOL_REGISTRY

    def test_schema_has_index_type_enum(self):
        """Test that schema has index_type parameter with enum."""
        tool = TOOL_REGISTRY["add_golden_repo_index"]
        assert "inputSchema" in tool
        assert "properties" in tool["inputSchema"]
        assert "index_type" in tool["inputSchema"]["properties"]
        assert "enum" in tool["inputSchema"]["properties"]["index_type"]

    def test_schema_uses_individual_types_not_combined(self):
        """
        CRITICAL-3: Test that schema uses individual types, not combined semantic_fts.

        Expected types: semantic, fts, temporal, scip
        NOT: semantic_fts (old combined type)
        """
        tool = TOOL_REGISTRY["add_golden_repo_index"]
        enum_values = tool["inputSchema"]["properties"]["index_type"]["enum"]

        # Assert old combined type is NOT present
        assert "semantic_fts" not in enum_values, (
            "CRITICAL: MCP schema still uses old 'semantic_fts' combined type. "
            "Should use separate 'semantic' and 'fts' types."
        )

        # Assert new individual types ARE present
        assert "semantic" in enum_values, "Missing 'semantic' type in MCP schema"
        assert "fts" in enum_values, "Missing 'fts' type in MCP schema"
        assert "temporal" in enum_values, "Missing 'temporal' type in MCP schema"
        assert "scip" in enum_values, "Missing 'scip' type in MCP schema"

    def test_schema_description_references_individual_types(self):
        """Test that description references individual types correctly."""
        tool = TOOL_REGISTRY["add_golden_repo_index"]
        description = tool.get("description", "")

        # Description should mention the individual types
        # It should NOT prominently feature semantic_fts as a valid option
        assert (
            "semantic_fts" not in description or "deprecated" in description.lower()
        ), (
            "Description still references semantic_fts as valid option. "
            "Should describe individual types: semantic, fts, temporal, scip."
        )


class TestGetGoldenRepoIndexesSchema:
    """Tests for get_golden_repo_indexes MCP tool schema."""

    def test_schema_exists_in_registry(self):
        """Test that get_golden_repo_indexes tool is registered."""
        assert "get_golden_repo_indexes" in TOOL_REGISTRY

    def test_output_schema_uses_individual_types(self):
        """
        HIGH-1: Test that output schema references individual types.

        Response should show separate semantic and fts status,
        not combined semantic_fts.
        """
        tool = TOOL_REGISTRY["get_golden_repo_indexes"]
        description = tool.get("description", "")

        # Description should reference individual index types
        # If it prominently references semantic_fts, it's outdated
        if "semantic_fts" in description and "deprecated" not in description.lower():
            pytest.fail(
                "get_golden_repo_indexes description references 'semantic_fts'. "
                "Should describe separate 'semantic' and 'fts' status."
            )


class TestTriggerReindexSchema:
    """Tests for trigger_reindex MCP tool schema if it exists."""

    def test_schema_uses_individual_types(self):
        """Test that trigger_reindex uses individual types."""
        if "trigger_reindex" not in TOOL_REGISTRY:
            pytest.skip("trigger_reindex tool not in registry")

        tool = TOOL_REGISTRY["trigger_reindex"]
        input_schema = tool.get("inputSchema", {})
        properties = input_schema.get("properties", {})

        # Check index_types parameter if present
        if "index_types" in properties:
            enum_values = properties["index_types"].get("items", {}).get("enum", [])
            if enum_values:
                assert (
                    "semantic_fts" not in enum_values
                ), "trigger_reindex uses old 'semantic_fts' type"
                assert "semantic" in enum_values or not enum_values


class TestAllToolsSchemasConsistency:
    """Tests for consistency across all MCP tool schemas."""

    def test_no_tool_uses_semantic_fts_in_enum(self):
        """
        Ensure no MCP tool schema uses deprecated semantic_fts in enum values.
        """
        tools_with_semantic_fts = []

        for tool_name, tool_def in TOOL_REGISTRY.items():
            input_schema = tool_def.get("inputSchema", {})
            if isinstance(input_schema, dict):
                self._check_schema_for_semantic_fts(
                    tool_name, input_schema, tools_with_semantic_fts
                )

        if tools_with_semantic_fts:
            pytest.fail(
                f"The following tools still use 'semantic_fts' in enum: "
                f"{', '.join(tools_with_semantic_fts)}. "
                f"Should use separate 'semantic' and 'fts' types."
            )

    def _check_schema_for_semantic_fts(
        self, tool_name: str, schema: dict, found_list: list
    ):
        """Recursively check schema for semantic_fts in enums."""
        if "enum" in schema:
            if "semantic_fts" in schema["enum"]:
                found_list.append(tool_name)

        for key, value in schema.items():
            if isinstance(value, dict):
                self._check_schema_for_semantic_fts(tool_name, value, found_list)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self._check_schema_for_semantic_fts(tool_name, item, found_list)
