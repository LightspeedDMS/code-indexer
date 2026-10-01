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

# Tools whose handlers (scip.py) read and use the "project" filter parameter.
SCIP_TOOLS_WITH_PROJECT_FILTER = [
    "scip_definition",
    "scip_references",
    "scip_dependencies",
    "scip_dependents",
]

# Tools whose handlers never read "project" -- removed from schema (bug #1356).
SCIP_TOOLS_WITHOUT_PROJECT_FILTER = [
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
        assert "repository_alias" in properties, (
            f"Tool {tool_name} missing repository_alias in inputSchema.properties"
        )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_accepts_string_type(self, tool_name: str):
        """AC2: repository_alias accepts string type for single repo filter."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Should accept string type
        type_def = repo_alias_schema.get("type")
        if isinstance(type_def, list):
            assert "string" in type_def, (
                f"Tool {tool_name}: repository_alias should accept string type"
            )
        elif isinstance(type_def, str):
            assert type_def == "string", (
                f"Tool {tool_name}: repository_alias should be string type"
            )
        else:
            # Could be using oneOf pattern
            one_of = repo_alias_schema.get("oneOf", [])
            types = [opt.get("type") for opt in one_of]
            assert "string" in types, (
                f"Tool {tool_name}: repository_alias should accept string type"
            )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_accepts_null_type(self, tool_name: str):
        """AC2/AC5: repository_alias accepts null (for all repos default behavior)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Should accept null type
        type_def = repo_alias_schema.get("type")
        if isinstance(type_def, list):
            assert "null" in type_def, (
                f"Tool {tool_name}: repository_alias should accept null type"
            )
        else:
            # Could be using oneOf pattern
            one_of = repo_alias_schema.get("oneOf", [])
            types = [opt.get("type") for opt in one_of]
            # Check if null is in types or if default is None
            has_null = "null" in types or repo_alias_schema.get("default") is None
            assert has_null, (
                f"Tool {tool_name}: repository_alias should accept null or have null default"
            )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_has_null_default(self, tool_name: str):
        """AC5: Default behavior preserved (null = all repos)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        # Default should be None/null for all repos behavior
        assert repo_alias_schema.get("default") is None, (
            f"Tool {tool_name}: repository_alias default should be null/None"
        )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_has_description(self, tool_name: str):
        """AC6: Description documents repository_alias parameter usage."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        repo_alias_schema = schema["properties"]["repository_alias"]

        assert "description" in repo_alias_schema, (
            f"Tool {tool_name}: repository_alias should have description"
        )

        description = repo_alias_schema["description"]
        assert len(description) > 10, (
            f"Tool {tool_name}: repository_alias description too short"
        )
        # Description should mention filtering/repo
        assert "repo" in description.lower(), (
            f"Tool {tool_name}: description should mention repository"
        )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS)
    def test_repository_alias_not_required(self, tool_name: str):
        """AC5: repository_alias is optional (not in required list)."""
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        required = schema.get("required", [])

        assert "repository_alias" not in required, (
            f"Tool {tool_name}: repository_alias should be optional, not required"
        )


class TestSCIPToolSchemasExistence:
    """Verify all expected SCIP tools exist in registry."""

    def test_all_scip_tools_registered(self):
        """All 7 SCIP tools exist in TOOL_REGISTRY."""
        for tool_name in SCIP_TOOLS:
            assert tool_name in TOOL_REGISTRY, (
                f"SCIP tool {tool_name} not found in TOOL_REGISTRY"
            )

    def test_scip_tools_count(self):
        """Exactly 7 SCIP tools expected."""
        assert len(SCIP_TOOLS) == 7, "Expected exactly 7 SCIP tools"


class TestSCIPCallchainMaxDepthSchema:
    """Bug #1603 code review (Priority 2, item 1; round 2 Priority 3 item A):
    scip_callchain's inputSchema is the LIVE MCP contract (TOOL_REGISTRY
    reads it directly from scip_callchain.md's YAML frontmatter) -- it must
    advertise the real [1, 3] max_depth cap the handler now enforces, not
    the stale [1, 10] contract from before this bug's remediation. Pins the
    schema so it cannot silently drift back, mirroring Bug #1356's
    schema-pin precedent above.

    Round 2: previously hardcoded the literal `3` instead of importing
    _MAX_CALLCHAIN_DEPTH from handlers/scip.py, so these tests would keep
    passing even if the handler's constant changed and the doc did not --
    now imports the real constant so the pin is genuine doc-to-code
    agreement, not two independent literals that happen to match today.
    """

    def test_max_depth_default_is_3(self):
        from code_indexer.server.mcp.handlers.scip import _MAX_CALLCHAIN_DEPTH

        max_depth_schema = TOOL_REGISTRY["scip_callchain"]["inputSchema"]["properties"][
            "max_depth"
        ]
        assert max_depth_schema["default"] == _MAX_CALLCHAIN_DEPTH, (
            "scip_callchain's advertised max_depth default must match "
            "_MAX_CALLCHAIN_DEPTH in handlers/scip.py "
            f"({_MAX_CALLCHAIN_DEPTH}), got: {max_depth_schema['default']}"
        )

    def test_max_depth_description_documents_cap_of_3(self):
        from code_indexer.server.mcp.handlers.scip import _MAX_CALLCHAIN_DEPTH

        max_depth_schema = TOOL_REGISTRY["scip_callchain"]["inputSchema"]["properties"][
            "max_depth"
        ]
        description = max_depth_schema.get("description", "").lower()
        assert f"max {_MAX_CALLCHAIN_DEPTH}" in description, (
            "scip_callchain's max_depth description must explicitly "
            f"document the real cap ('Max {_MAX_CALLCHAIN_DEPTH}'), got: "
            f"{description!r}"
        )
        assert "max 10" not in description, (
            "scip_callchain's max_depth description must not still "
            f"advertise the stale [1, 10] contract, got: {description!r}"
        )

    def test_max_depth_schema_has_minimum_and_maximum_bounds(self):
        """Bug #1603 round 2 Priority 3 item A: the YAML schema itself must
        machine-enforce the cap via minimum/maximum keys, not just prose in
        the description -- an MCP client validating against the advertised
        JSON schema would otherwise still accept max_depth: 10 and only get
        rejected at runtime by the handler. Mirrors the minimum/maximum
        pattern already used by search/search_code.md's edit_distance field.
        """
        from code_indexer.server.mcp.handlers.scip import _MAX_CALLCHAIN_DEPTH

        max_depth_schema = TOOL_REGISTRY["scip_callchain"]["inputSchema"]["properties"][
            "max_depth"
        ]
        assert max_depth_schema.get("minimum") == 1, (
            "scip_callchain's max_depth schema must advertise minimum: 1, "
            f"got: {max_depth_schema.get('minimum')!r}"
        )
        assert max_depth_schema.get("maximum") == _MAX_CALLCHAIN_DEPTH, (
            "scip_callchain's max_depth schema must advertise maximum "
            f"matching _MAX_CALLCHAIN_DEPTH ({_MAX_CALLCHAIN_DEPTH}), got: "
            f"{max_depth_schema.get('maximum')!r}"
        )


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

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS_WITH_PROJECT_FILTER)
    def test_existing_project_filter_preserved(self, tool_name: str):
        """Existing project filter parameter is preserved.

        These 4 tools' handlers (scip.py) read and use the project
        parameter, so it must remain in their schemas.
        """
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        properties = schema["properties"]

        assert "project" in properties, (
            f"Tool {tool_name}: existing project filter should be preserved"
        )

    @pytest.mark.parametrize("tool_name", SCIP_TOOLS_WITHOUT_PROJECT_FILTER)
    def test_project_filter_not_supported_for_non_reading_tools(self, tool_name: str):
        """Project filter is absent for tools whose handlers never read it.

        Bug #1356: scip_impact, scip_callchain, and scip_context handlers
        in scip.py never read the project parameter (unlike
        scip_definition/references/dependencies/dependents, which do), so
        it was removed from their schemas to avoid documenting a parameter
        that has no effect.
        """
        schema = TOOL_REGISTRY[tool_name]["inputSchema"]
        properties = schema["properties"]

        assert "project" not in properties, (
            f"Tool {tool_name}: project filter should not be present "
            "since the handler never reads it (bug #1356)"
        )
