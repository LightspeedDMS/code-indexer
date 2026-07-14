---
name: get_tool_categories
category: guides
required_permission: query_repos
tl_dr: Get organized list of all available MCP tools grouped by category.
slim_description: "Return all available MCP tools organized by category (search, git, files, SCIP, admin, etc.) with no parameters required."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Always true (present on every response)
    categories:
      type: object
      description: Tools organized by category. Category keys are UPPER-CASED (e.g. "SEARCH", "GIT"), not the lowercase names used elsewhere (e.g. in cidx_quick_reference's category filter).
      additionalProperties:
        type: array
        items:
          type: string
    total_tools:
      type: integer
      description: Total number of tools available
  required:
  - success
  - categories
  - total_tools
---

Returns list of all available MCP tools grouped by category (search, git, files, SCIP, admin, etc.). Category keys in the response are UPPER-CASED (e.g. "SEARCH", "GIT"). Call to discover what tools are available and explore related tools within a category.

EXAMPLE: cidx_quick_reference(category='search') for detailed guidance on a specific category.
