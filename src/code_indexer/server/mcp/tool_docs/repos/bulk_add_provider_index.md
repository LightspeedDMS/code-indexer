---
name: bulk_add_provider_index
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Add a provider''s semantic index to every golden repository that lacks it.'
slim_description: "[ADMIN ONLY] Add a provider's semantic index to the golden repositories that currently lack it, with an optional category filter pattern. Requires MCP elevation (TOTP step-up) when enforcement is on."
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Embedding provider name to add indexes for"
    filter:
      type: string
      description: "Optional filter. The only supported form is 'category:<name>': the 'category:' prefix is case-insensitive, <name> must be non-empty and is matched exactly and case-sensitively against the repository's category name. Any other value is rejected with an error and no jobs are created. Omit (or pass an empty string) to target every repository."
  required:
    - provider
  additionalProperties: false
---
[ADMIN ONLY] Bulk add a provider's semantic index to the golden repositories that lack it. Matches the REST twin (POST .../bulk-add): the admin role is required, and so is an active elevation window when elevation enforcement is turned on.

Creates background jobs for each repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally restrict the operation to one repository category with `filter="category:<name>"`.

FILTER RULES:
- The only supported filter is `category:<name>`. The `category:` prefix is case-insensitive.
- `<name>` is matched exactly and case-sensitively against the category name (category names are stored case-sensitively, so `Backend` and `backend` are different categories). `category:back` does not match `Backend`.
- Any other filter, or an empty name (`category:`), is rejected with an error before any repository config is written or any job is created.

ERRORS:
- Unsupported filter / empty category name: validation error, no jobs created
- Repository category service not available: a category filter cannot be applied, no jobs created
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

Examples:
- Add to all repos: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
