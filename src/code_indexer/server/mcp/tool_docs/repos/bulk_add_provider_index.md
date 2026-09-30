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
      description: "Optional filter pattern. ONLY the 'category:<name>' prefix is honored (case-insensitive substring match against the repo's category). Any other value or prefix is silently ignored (no filtering applied, no error)."
  required:
    - provider
  additionalProperties: false
---
[ADMIN ONLY] Bulk add a provider's semantic index to the golden repositories that lack it. Matches the REST twin (POST .../bulk-add): the admin role is required, and so is an active elevation window when elevation enforcement is turned on.

Creates background jobs for each repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally filter repositories by category pattern.

LIMITATION: The `filter` parameter ONLY recognizes the `category:<name>` prefix. Any other filter string (or an unrecognized prefix) is silently a no-op -- all eligible repositories are processed as if no filter were given, with no error returned.

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

Examples:
- Add to all repos: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
