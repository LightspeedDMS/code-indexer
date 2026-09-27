---
name: bulk_add_provider_index
category: repos
required_permission: repository:write
tl_dr: Add a provider's semantic index to the golden repositories you can access that lack it.
slim_description: "Add a provider's semantic index to the golden repositories the caller can access that currently lack it, with an optional category filter pattern. Requires MCP elevation (TOTP step-up) when enforcement is on."
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
Bulk add a provider's semantic index to the golden repositories the caller can access that lack it. Admins see and target every golden repository; non-admin callers only see and target the repositories their group grants them access to.

Requires MCP elevation (TOTP step-up) when elevation enforcement is turned on, matching the REST twin (POST .../bulk-add).

Creates background jobs for each accessible repository missing the specified provider's index. Returns list of job IDs for progress tracking.

Optionally filter repositories by category pattern.

LIMITATION: The `filter` parameter ONLY recognizes the `category:<name>` prefix. Any other filter string (or an unrecognized prefix) is silently a no-op -- all eligible repositories are processed as if no filter were given, with no error returned.

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

Examples:
- Add to all accessible repos: `bulk_add_provider_index(provider="cohere")`
- Add to accessible backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
