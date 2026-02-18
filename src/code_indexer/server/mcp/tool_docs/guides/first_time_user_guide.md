---
name: first_time_user_guide
category: guides
required_permission: query_repos
tl_dr: Get step-by-step quick start guide for new CIDX MCP server users.
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    guide:
      type: object
      properties:
        steps:
          type: array
          items:
            type: object
            properties:
              step_number:
                type: integer
              title:
                type: string
              description:
                type: string
              example_call:
                type: string
              expected_result:
                type: string
        quick_start_summary:
          type: array
          items:
            type: string
          description: One-line summary of each step for quick reference
        common_errors:
          type: array
          items:
            type: object
            properties:
              error:
                type: string
              solution:
                type: string
---

Step-by-step guide for new CIDX users. Call with no arguments to receive the full guide.

KEY WORKFLOW: (1) list_global_repos() to see available repositories, (2) search cidx-meta-global to discover which repo has your topic, (3) search the identified repo for actual code.

CRITICAL: If you don't know which repo to search, ALWAYS search cidx-meta-global FIRST. This returns .md files describing what each repository contains.

For multi-repo search: repository_alias=['repo1-global', 'repo2-global'] with aggregation_mode='global' for best matches, or 'per_repo' for balanced representation across repos.

Call this tool to receive detailed step-by-step instructions with examples.
