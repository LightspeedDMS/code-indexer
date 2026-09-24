"""MCP Tool Registry - Dynamically built from markdown documentation files.

This module provides:
- TOOL_REGISTRY: Dictionary of all tool definitions built from .md files
- filter_tools_by_role: Filters tools based on user permissions

The tool definitions (name, description, inputSchema, required_permission) are
loaded from markdown files in the tool_docs/ directory. Each .md file contains
YAML frontmatter with the tool's inputSchema and metadata.

This approach establishes markdown files as the SINGLE SOURCE OF TRUTH for:
- Tool descriptions (markdown body)
- Input schemas (inputSchema in frontmatter)
- Required permissions (required_permission in frontmatter)
- Category organization (category in frontmatter)
"""

from pathlib import Path
from typing import List, Dict, Any

from code_indexer.server.auth.user_manager import User
from .tool_access import _ALWAYS_AVAILABLE_TOOLS


# =============================================================================
# REPOSITORY DISCOVERY WORKFLOW (HIGH PRIORITY)
# =============================================================================
# See tool_docs/guides/repository_discovery_workflow.md for complete documentation
#
# CRITICAL: Unless the user explicitly specifies which repository to search,
# FIRST search cidx-meta-global to discover which repositories are relevant.
#
# MANDATORY WORKFLOW:
# 1. search_code('topic', repository_alias='cidx-meta-global')
#    -> Returns .md files describing each repository's contents
#
# 2. Then search the specific repositories identified
#    -> search_code('detailed query', repository_alias='identified-repo-global')
#
# EXCEPTION: Skip cidx-meta discovery ONLY if user explicitly names a repository.
# =============================================================================

# =============================================================================
# PERMISSION SYSTEM OVERVIEW
# =============================================================================
# See tool_docs/guides/permission_system.md for complete documentation
#
# Permission              | Roles                              | Grants Access To
# ------------------------|------------------------------------|-----------------
# query_repos             | normal_user, power_user, admin     | search, browse, read
# activate_repos          | power_user, admin                  | activate/deactivate repos
# repository:read         | normal_user, power_user, admin     | git_status, read ops
# repository:write        | power_user, admin                  | file CRUD, git write
# repository:admin        | admin                              | repo management
# manage_golden_repos     | admin                              | add/remove global repos
# manage_users            | admin                              | user management
# =============================================================================


def _build_registry() -> Dict[str, Dict[str, Any]]:
    """Build TOOL_REGISTRY from markdown documentation files.

    This function is called once at module load time to build the registry.
    The ToolDocLoader reads all .md files from tool_docs/ and extracts:
    - name, category, required_permission from frontmatter
    - description from markdown body
    - inputSchema from frontmatter

    Returns:
        Dict mapping tool names to their complete definitions.
    """
    from code_indexer.server.mcp.tool_doc_loader import ToolDocLoader

    docs_dir = Path(__file__).parent / "tool_docs"
    loader = ToolDocLoader(docs_dir)
    return loader.build_tool_registry()  # type: ignore[no-any-return]


# Build TOOL_REGISTRY at module load time
TOOL_REGISTRY: Dict[str, Dict[str, Any]] = _build_registry()


def tool_passes_config_gate(tool_def: Dict[str, Any], config: Any) -> bool:
    """Story #185: whether `tool_def`'s optional `requires_config` condition
    is satisfied by `config` -- e.g. a tracing tool declaring
    `requires_config: langfuse_enabled` is gated on `config.langfuse_config
    .enabled`. A tool with no `requires_config` always passes.

    Bug #1955 item 6: extracted out of `filter_tools_by_role` (the ONLY
    place this gate was applied) so `guides.py`'s tool-COUNTING helpers
    (`quick_reference`, `get_tool_categories`) can apply the IDENTICAL
    gate -- before this fix, `cidx_quick_reference()`'s `total_tools`
    counted every Langfuse-gated tool unconditionally, silently disagreeing
    with what the real `tools/list` (this function) would actually serve
    on a deployment with Langfuse disabled (Rule 4, anti-duplication: one
    gate, never two copies that can drift).
    """
    requires_config = tool_def.get("requires_config")
    if not requires_config:
        return True
    if not config:
        return False  # Fail-closed: no config available.
    if requires_config == "langfuse_enabled":
        return bool(config.langfuse_config.enabled)
    # Add more config checks here as needed.
    return True


def filter_tools_by_role(
    user: User,
    config=None,
    *,
    session_state=None,
    tool_access_memo=None,
) -> List[Dict[str, Any]]:
    """
    Filter tools based on user role, permissions, and configuration requirements.

    Story #185: Tools can specify requires_config in their frontmatter to conditionally
    appear based on server configuration. For example, tracing tools require Langfuse
    to be enabled.

    Args:
        user: Authenticated user with role information
        config: Optional Config object for checking configuration requirements

    Returns:
        List of MCP-compliant tool definitions (name, description, inputSchema only)
    """
    from .tool_access import ToolAccessMemo, resolve_effective_user

    if tool_access_memo is None:
        tool_access_memo = ToolAccessMemo()
    effective_user = resolve_effective_user(user, session_state)
    filtered_tools = []

    for tool_name, tool_def in TOOL_REGISTRY.items():
        required_permission = tool_def["required_permission"]
        group_decision = (
            True
            if tool_name in _ALWAYS_AVAILABLE_TOOLS
            else tool_access_memo.is_allowed(tool_name, effective_user)
        )
        if group_decision is False:
            continue
        if group_decision is None and not effective_user.has_permission(
            required_permission
        ):
            continue

        # Story #185: Check if tool requires specific configuration
        if not tool_passes_config_gate(tool_def, config):
            continue

        # Only include MCP-valid fields (name, description, inputSchema)
        # Filter out internal fields (required_permission, outputSchema, requires_config)
        mcp_tool = {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "inputSchema": tool_def["inputSchema"],
        }
        filtered_tools.append(mcp_tool)

    return filtered_tools
