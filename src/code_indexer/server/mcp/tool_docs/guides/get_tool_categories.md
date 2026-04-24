---
name: get_tool_categories
category: guides
required_permission: query_repos
tl_dr: Get organized list of all available MCP tools grouped by category.
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    categories:
      type: object
      description: Tools organized by category
      additionalProperties:
        type: array
        items:
          type: string
    total_tools:
      type: integer
      description: Total number of tools available
---

Returns list of all available MCP tools grouped by category (search, git, files, SCIP, admin, etc.). Call to discover what tools are available and explore related tools within a category.

EXAMPLE: cidx_quick_reference(category='search') for detailed guidance on a specific category.
