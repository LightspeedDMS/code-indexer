---
name: list_repo_categories
category: repos
required_permission: query_repos
tl_dr: List all repository categories with their patterns and priorities.
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    categories:
      type: array
      description: List of repository categories ordered by priority
      items:
        type: object
        properties:
          id:
            type: integer
            description: Category unique identifier
          name:
            type: string
            description: Category name
          pattern:
            type: string
            description: Regex pattern for auto-assignment
          priority:
            type: integer
            description: Display order priority (lower numbers appear first)
          created_at:
            type: string
            description: ISO 8601 timestamp when category was created
          updated_at:
            type: string
            description: ISO 8601 timestamp when category was last updated
    total:
      type: integer
      description: Total number of categories
    error:
      type: string
      description: Error message if failed
  required:
  - success
  - categories
  - total
---

Lists all repository categories with their patterns and priorities. Categories are used to organize repositories in the UI and filter repository listings. Each category has a regex pattern used for automatic assignment when repositories are registered.

Categories are returned in priority order (ascending), which determines their display order in the UI. Lower priority numbers appear first.

USE CASES: Discover available categories for filtering repositories. Understand category patterns for troubleshooting auto-assignment. Check category priorities for display order.
