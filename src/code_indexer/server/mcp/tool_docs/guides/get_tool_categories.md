---
name: get_tool_categories
category: guides
required_permission: null
tl_dr: Get organized list of all available MCP tools grouped by category.
---

TL;DR: Get organized list of all available MCP tools grouped by category. Use this to discover what tools are available and what each category offers.

USE CASES:
(1) New user wanting to see what CIDX can do
(2) Looking for tools in a specific category (search, git, file operations, etc.)
(3) Discovering related tools when you know one tool in a category

RETURNS: Tools organized into logical categories with one-line descriptions.

CATEGORIES INCLUDED:
- SEARCH & DISCOVERY: Code search, browsing, file exploration
- GIT HISTORY & EXPLORATION: Commit history, blame, diffs, temporal queries
- GIT OPERATIONS: Stage, commit, push, pull, branch management
- FILE CRUD: Create, edit, delete files in activated repositories
- SCIP CODE INTELLIGENCE: Find definitions, references, call chains, dependencies
- REPOSITORY MANAGEMENT: Activate, deactivate, sync repositories
- SYSTEM & ADMIN: User management, repository administration, system info

EXAMPLE OUTPUT:
{
  "categories": {
    "SEARCH & DISCOVERY": [
      "search_code - Semantic/FTS code search across repositories",
      "regex_search - Pattern matching without requiring indexes",
      "browse_directory - List files with metadata and filtering",
      ...
    ],
    ...
  },
  "total_tools": 55
}

RELATED TOOLS:
- cidx_quick_reference: Decision guide for choosing tools
- first_time_user_guide: Step-by-step getting started guide
