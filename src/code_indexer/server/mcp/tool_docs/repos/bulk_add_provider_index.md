---
name: bulk_add_provider_index
category: repos
required_permission: repository:write
tl_dr: Add a provider's semantic index to all repositories that lack it.
slim_description: "Add a provider's semantic index to all golden repositories that currently lack it, with an optional category filter pattern."
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Embedding provider name to add indexes for"
    filter:
      type: string
      description: "Optional filter pattern. ONLY the 'category:<name>' prefix is honored (case-insensitive substring match against the repo's category). Any other value or prefix is silently ignored (no filtering applied, no error)."
  required:
    - provider
  additionalProperties: false
---
Bulk add a provider's semantic index to all golden repositories that lack it.

Creates background jobs for each repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally filter repositories by category pattern.

LIMITATION: The `filter` parameter ONLY recognizes the `category:<name>` prefix. Any other filter string (or an unrecognized prefix) is silently a no-op -- all eligible repositories are processed as if no filter were given, with no error returned.

Examples:
- Add to all: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
