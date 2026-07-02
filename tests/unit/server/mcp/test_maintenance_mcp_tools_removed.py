"""Tests verifying MCP maintenance enter/exit tools are removed (Story #924 AC3).

Maintenance enter/exit are now localhost-only HTTP endpoints driven by the
auto-updater. MCP is a network-accessible surface and cannot satisfy the
localhost restriction, so the enter/exit tools must be absent.

Verifies:
- enter_maintenance_mode and exit_maintenance_mode are NOT in TOOL_REGISTRY
- No tool doc .md files exist for enter/exit maintenance
"""

from pathlib import Path


_TOOL_DOCS_ADMIN = (
    Path(__file__).parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "mcp"
    / "tool_docs"
    / "admin"
)


def test_enter_and_exit_maintenance_not_in_tool_registry():
    """Neither enter_maintenance_mode nor exit_maintenance_mode may be in TOOL_REGISTRY."""
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    assert "enter_maintenance_mode" not in TOOL_REGISTRY
    assert "exit_maintenance_mode" not in TOOL_REGISTRY


def test_no_maintenance_tool_docs():
    """enter_maintenance_mode.md and exit_maintenance_mode.md must not exist in tool_docs."""
    enter_doc = _TOOL_DOCS_ADMIN / "enter_maintenance_mode.md"
    exit_doc = _TOOL_DOCS_ADMIN / "exit_maintenance_mode.md"
    assert not enter_doc.exists(), f"Found unexpected tool doc: {enter_doc}"
    assert not exit_doc.exists(), f"Found unexpected tool doc: {exit_doc}"
