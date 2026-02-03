"""
Backward compatibility tests for tool documentation externalization.

Story #14: Externalize MCP Tool Documentation to Markdown Files
AC9: Backward Compatibility - Verify character-for-character identical output.
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


class TestBackwardCompatibility:
    """Tests for character-for-character backward compatibility (AC9)."""

    def test_loaded_description_matches_registry_exactly(self, temp_docs_dir):
        """Loaded description should match TOOL_REGISTRY description exactly."""
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Convert all tools to .md files
        convert_all_tools(TOOL_REGISTRY, temp_docs_dir)

        # Load them back
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        # Verify each tool's description matches exactly
        mismatches = []
        for tool_name, tool_def in TOOL_REGISTRY.items():
            original_description = tool_def.get("description", "")
            loaded_description = loader.get_description(tool_name)

            if original_description != loaded_description:
                mismatches.append(
                    {
                        "tool": tool_name,
                        "original_len": len(original_description),
                        "loaded_len": len(loaded_description),
                        "diff_start": self._find_first_diff(
                            original_description, loaded_description
                        ),
                    }
                )

        if mismatches:
            details = "\n".join(
                f"  {m['tool']}: orig={m['original_len']}, loaded={m['loaded_len']}, diff@{m['diff_start']}"
                for m in mismatches[:10]
            )
            pytest.fail(f"Found {len(mismatches)} description mismatches:\n{details}")

    def test_all_tools_have_identical_descriptions(self, temp_docs_dir):
        """All tools should have identical descriptions after round-trip."""
        from tools.convert_tool_docs import convert_all_tools
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Convert and load
        convert_all_tools(TOOL_REGISTRY, temp_docs_dir)
        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()

        # Count exact matches
        exact_matches = 0
        expected_count = len(TOOL_REGISTRY)
        for tool_name, tool_def in TOOL_REGISTRY.items():
            original = tool_def.get("description", "")
            loaded = loader.get_description(tool_name)
            if original == loaded:
                exact_matches += 1

        assert (
            exact_matches == expected_count
        ), f"Only {exact_matches}/{expected_count} tools have exact description matches"

    def test_whitespace_preserved_exactly(self, temp_docs_dir):
        """Whitespace (newlines, spaces, tabs) should be preserved exactly."""
        from tools.convert_tool_docs import convert_tool
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        # Create a tool with specific whitespace patterns
        description_with_whitespace = """TL;DR: Test tool.

This has:
  - Indented list
  - Another item

And multiple

Blank lines."""

        tool_def = {
            "description": description_with_whitespace,
            "required_permission": "query_repos",
        }

        convert_tool("whitespace_tool", tool_def, temp_docs_dir, "search")

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        loaded = loader.get_description("whitespace_tool")

        assert loaded == description_with_whitespace, (
            f"Whitespace not preserved:\n"
            f"Original: {repr(description_with_whitespace)}\n"
            f"Loaded: {repr(loaded)}"
        )

    def test_special_characters_preserved(self, temp_docs_dir):
        """Special characters (quotes, brackets, etc.) should be preserved."""
        from tools.convert_tool_docs import convert_tool
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        description_with_special = """TL;DR: Test special chars.

Examples: "quoted", 'single', `backticks`
Brackets: [array], {object}, (parens)
Symbols: @decorator, #comment, $var, %percent
Code: def func(): pass"""

        tool_def = {
            "description": description_with_special,
            "required_permission": "query_repos",
        }

        convert_tool("special_chars_tool", tool_def, temp_docs_dir, "search")

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        loaded = loader.get_description("special_chars_tool")

        assert loaded == description_with_special

    def test_unicode_characters_preserved(self, temp_docs_dir):
        """Unicode characters should be preserved."""
        from tools.convert_tool_docs import convert_tool
        from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

        description_with_unicode = (
            "TL;DR: Unicode test. Arrow: \u2192, Check: \u2713, Euro: \u20ac"
        )

        tool_def = {
            "description": description_with_unicode,
            "required_permission": "query_repos",
        }

        convert_tool("unicode_tool", tool_def, temp_docs_dir, "search")

        loader = ToolDocLoader(temp_docs_dir)
        loader.load_all_docs()
        loaded = loader.get_description("unicode_tool")

        assert loaded == description_with_unicode

    def _find_first_diff(self, s1: str, s2: str) -> int:
        """Find the index of the first differing character."""
        for i, (c1, c2) in enumerate(zip(s1, s2)):
            if c1 != c2:
                return i
        return min(len(s1), len(s2))
