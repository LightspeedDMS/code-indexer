# MCP Capabilities Permissions Matrix

Last Updated: 2026-02-12

## Overview

This document provides a comprehensive mapping of MCP tools to required permissions and user roles. Use this matrix to understand which capabilities are available to different user types.

## User Roles

CIDX defines three user roles with cumulative permissions:

| Role | Permissions | Description |
|------|-------------|-------------|
| **ADMIN** | manage_users, manage_golden_repos, activate_repos, query_repos | Full system access - user management, golden repo administration, repository activation, and querying |
| **POWER_USER** | activate_repos, query_repos | Repository activation and querying - can activate/sync repos and perform all search operations |
| **NORMAL_USER** | query_repos | Read-only access - search, browse, and view repository data |

Source: `src/code_indexer/server/auth/user_manager.py:7-28`

## Permission Levels

| Permission | Description | Tool Count |
|------------|-------------|------------|
| `public` | No authentication required | 1 |
| `query_repos` | Read-only repository access (search, browse, git history) | ~100 |
| `activate_repos` | Repository activation and management | ~15 |
| `manage_golden_repos` | Golden repository administration | ~10 |
| `manage_users` | User account management | ~5 |

Total MCP Tools: 131

Note: Tool counts per permission are approximate. Tools are loaded dynamically from markdown files in `src/code_indexer/server/mcp/tool_docs/`.

## Complete Tool-Permission-Role Matrix

### Public Tools (No Authentication)

| Tool | Permission | ADMIN | POWER_USER | NORMAL_USER | Description |
|------|------------|-------|------------|-------------|-------------|
| `authenticate` | public | ✓ | ✓ | ✓ | Authenticate and obtain access token |

### Query Tools (All Users)

All authenticated users (ADMIN, POWER_USER, NORMAL_USER) have access to the majority of tools (approximately 100+ tools with `query_repos` permission). Key examples include:

| Tool | Category | Description |
|------|----------|-------------|
| `search_code` | Search | Semantic/FTS/hybrid code search with 25 parameters |
| `discover_repositories` | Search | Discover indexed repositories |
| `list_repositories` | Repository | List all repositories |
| `get_repository_status` | Repository | Get activation status for repository |
| `get_all_repositories_status` | Repository | Get status for all repositories |
| `get_repository_statistics` | Analytics | Get repository statistics (files, chunks, languages) |
| `list_files` | Files | List all indexed files in repository |
| `get_file_content` | Files | Retrieve file content by path |
| `browse_directory` | Files | Browse directory structure |
| `directory_tree` | Files | Get directory tree structure |
| `get_branches` | Git | List available branches for repository |
| `git_log` | Git | View commit history with optional path filtering |
| `git_show_commit` | Git | Show detailed commit information |
| `git_file_at_revision` | Git | Get file content at specific revision |
| `git_diff` | Git | Compare commits, branches, or working tree |
| `git_blame` | Git | Show line-by-line commit attribution |
| `git_file_history` | Git | Get complete history for specific file |
| `git_search_commits` | Git | Search commit messages and metadata |
| `git_search_diffs` | Git | Search code changes across commits |
| `regex_search` | Search | Search file content using regex patterns |
| `check_health` | Health | Check server health and connectivity |
| `get_job_statistics` | Analytics | Get background job statistics |
| `get_job_details` | Analytics | Get detailed job status and results |
| `list_global_repos` | Repository | List globally-queryable repositories |
| `global_repo_status` | Repository | Get global repository status |
| `get_global_config` | Config | Retrieve global configuration settings |
| `get_golden_repo_indexes` | Repository | List available index types for golden repo |
| `scip_definition` | SCIP | Find symbol definition locations |
| `scip_references` | SCIP | Find all symbol references |
| `scip_dependencies` | SCIP | Get symbols that target depends on |
| `scip_dependents` | SCIP | Get symbols that depend on target |
| `scip_impact` | SCIP | Analyze change impact for symbol |
| `scip_callchain` | SCIP | Find call chains between symbols |
| `scip_context` | SCIP | Get smart context for symbol |
| `cidx_quick_reference` | Documentation | Quick reference guide for CIDX capabilities |
| ... | ... | Additional query tools (see tool_docs/ for complete list) |

### Activation Tools (POWER_USER and ADMIN)

POWER_USER and ADMIN have access to repository activation and management tools (approximately 15+ tools). Key examples include:

| Tool | Description |
|------|-------------|
| `activate_repository` | Activate repository for querying |
| `deactivate_repository` | Deactivate repository |
| `sync_repository` | Sync repository with latest changes |
| `switch_branch` | Switch active branch for repository |
| `manage_composite_repository` | Create/manage composite repositories |
| `cidx_ssh_key_create` | Create SSH key for git authentication |
| `cidx_ssh_key_list` | List available SSH keys |
| `cidx_ssh_key_delete` | Delete SSH key |
| `cidx_ssh_key_show_public` | Show public key content |
| `cidx_ssh_key_assign_host` | Assign SSH key to host |
| ... | ... | Additional activation tools (see tool_docs/ for complete list) |

### Golden Repo Administration (ADMIN Only)

ADMIN has exclusive access to golden repository management tools (approximately 10+ tools). Key examples include:

| Tool | Description |
|------|-------------|
| `add_golden_repo` | Add new golden repository |
| `remove_golden_repo` | Remove golden repository |
| `refresh_golden_repo` | Refresh golden repository |
| `add_golden_repo_index` | Add index to golden repository |
| `set_global_config` | Update global configuration |
| ... | ... | Additional admin tools (see tool_docs/ for complete list) |

### User Management (ADMIN Only)

ADMIN has exclusive access to user management tools (approximately 5+ tools). Key examples include:

| Tool | Description |
|------|-------------|
| `list_users` | List all users and their roles |
| `create_user` | Create new user account |
| ... | ... | Additional user management tools (see tool_docs/ for complete list) |

## Tool Count by Role

| Role | Tool Count | Breakdown |
|------|------------|-----------|
| **ADMIN** | 131 | All tools (1 public + ~100 query + ~15 activate + ~10 golden_repos + ~5 users) |
| **POWER_USER** | ~116 | 1 public + ~100 query + ~15 activate |
| **NORMAL_USER** | ~101 | 1 public + ~100 query |

Note: Tool counts are approximate. Exact counts depend on the current set of tools loaded dynamically from `src/code_indexer/server/mcp/tool_docs/`.

## Permission Checking

Permission checks occur at two levels:

1. **Tool Discovery** (`tools/list`): Users only see tools they have permission to use
   - Source: Permission filtering in MCP handlers based on user role

2. **Tool Invocation** (`tools/call`): Permission verified before execution
   - Source: Permission checking in MCP protocol implementation

Error response when permission denied:
```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": -32000,
    "message": "Permission denied: activate_repos required for tool activate_repository"
  },
  "id": 1
}
```

## Quick Reference by Use Case

### "I want to search code"
- **Required Role**: NORMAL_USER or higher
- **Key Tools**: `search_code`, `regex_search`, `scip_*` tools
- **Permission**: query_repos

### "I want to activate and sync repositories"
- **Required Role**: POWER_USER or higher
- **Key Tools**: `activate_repository`, `sync_repository`, `switch_branch`
- **Permission**: activate_repos

### "I want to add repositories for others to use"
- **Required Role**: ADMIN only
- **Key Tools**: `add_golden_repo`, `refresh_golden_repo`
- **Permission**: manage_golden_repos

### "I want to create user accounts"
- **Required Role**: ADMIN only
- **Key Tools**: `create_user`, `list_users`
- **Permission**: manage_users

## Related Documentation

- **API Reference**: `/docs/mcpb/api-reference.md` - Detailed parameter documentation for each tool
- **Setup Guide**: `/docs/mcpb/setup.md` - Installation and configuration instructions
- **Troubleshooting**: `/docs/mcpb/troubleshooting.md` - Common permission-related issues
- **User Management**: `/src/code_indexer/server/auth/user_manager.py` - Role and permission definitions
- **Tool Registry**: `/src/code_indexer/server/mcp/tools.py` - Complete tool definitions with permission requirements

## Version Information

- Last Updated: 2026-02-12
- CIDX Version: 8.13.0
- Total MCP Tools: 131
- Permission Model: Role-Based Access Control (RBAC)
- Tool Source: Dynamic loading from src/code_indexer/server/mcp/tool_docs/
