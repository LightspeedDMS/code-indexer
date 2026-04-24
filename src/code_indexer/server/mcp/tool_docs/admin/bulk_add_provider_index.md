---
name: bulk_add_provider_index
category: admin
required_permission: repository:write
tl_dr: 'Bulk add a provider''s semantic index to all golden repositories that lack
  it.


  Creates background jobs for each repository missing the specified provider''s index.'
---

Bulk add a provider's semantic index to all golden repositories that lack it.

Creates background jobs for each repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally filter repositories by category pattern.

Examples:
- Add to all: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
