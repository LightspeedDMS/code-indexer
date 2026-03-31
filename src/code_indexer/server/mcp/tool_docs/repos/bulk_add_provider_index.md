---
name: bulk_add_provider_index
category: repos
required_permission: repository:write
tl_dr: Add a provider's semantic index to all repositories that lack it.
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Embedding provider name to add indexes for"
    filter:
      type: string
      description: "Optional filter pattern (e.g., 'category:backend') to limit which repos receive the index"
  required:
    - provider
  additionalProperties: false
---
Bulk add a provider's semantic index to all golden repositories that lack it.

Creates background jobs for each repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally filter repositories by category pattern.

Examples:
- Add to all: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
