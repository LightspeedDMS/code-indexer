---
name: bulk_add_provider_index
category: repos
required_permission: manage_golden_repos
<<<<<<< HEAD
tl_dr: '[ADMIN ONLY] Add a provider''s semantic index to every golden repository that lacks it.'
slim_description: "[ADMIN ONLY] Add a provider's semantic index to the golden repositories that currently lack it, with an optional category filter pattern. Requires MCP elevation (TOTP step-up) when enforcement is on."
=======
tl_dr: '[ADMIN ONLY] Add a provider''s semantic index to every global repository that lacks it.'
slim_description: "[ADMIN ONLY] Add a provider's semantic index to every global repository that lacks it, one background job per repository. Requires MCP elevation (TOTP step-up) when enforcement is on. Optionally restricted to one repository category with filter='category:<name>'."
>>>>>>> upstream/master
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Embedding provider name to add indexes for. Use manage_provider_indexes(action='list_providers') to see the configured providers."
    filter:
      type: string
<<<<<<< HEAD
      description: "Optional filter pattern. ONLY the 'category:<name>' prefix is honored (case-insensitive substring match against the repo's category). Any other value or prefix is silently ignored (no filtering applied, no error)."
=======
      description: "Optional filter. The only supported form is 'category:<name>': the 'category:' prefix is case-insensitive, <name> must be non-empty and is matched exactly and case-sensitively against the repository's category name. Any other value is rejected with an error and no jobs are created. Omit (or pass an empty string) to target every repository."
>>>>>>> upstream/master
  required:
    - provider
  additionalProperties: false
---
<<<<<<< HEAD
[ADMIN ONLY] Bulk add a provider's semantic index to the golden repositories that lack it. Matches the REST twin (POST .../bulk-add): the admin role is required, and so is an active elevation window when elevation enforcement is turned on.
=======
[ADMIN ONLY] Add a provider's semantic index to every globally activated repository that does not have one yet. The admin role is required, and so is an active elevation window when elevation enforcement is turned on (call `elevate_session` first).
>>>>>>> upstream/master

BEHAVIOUR:
1. Validates `provider` against the configured providers.
2. Walks the global repository registry. For each repository:
   - repositories whose index path cannot be resolved are left out of both `jobs` and `skipped`;
   - repositories that already have the provider's index are added to `skipped`;
   - otherwise the provider is written into the repository's base-clone config and an index job is submitted; if the base clone cannot be resolved or the config write fails, the repository is added to `skipped` instead.
3. Records one audit entry for the whole request.

Optionally restrict the operation to one repository category with `filter="category:<name>"`.

<<<<<<< HEAD
LIMITATION: The `filter` parameter ONLY recognizes the `category:<name>` prefix. Any other filter string (or an unrecognized prefix) is silently a no-op -- all eligible repositories are processed as if no filter were given, with no error returned.

ERRORS:
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)

Examples:
- Add to all repos: `bulk_add_provider_index(provider="cohere")`
- Add to backend repos: `bulk_add_provider_index(provider="cohere", filter="category:backend")`
=======
FILTER RULES:
- The only supported filter is `category:<name>`. The `category:` prefix is case-insensitive.
- `<name>` is matched exactly and case-sensitively against the category name (category names are stored case-sensitively, so `Backend` and `backend` are different categories). `category:back` does not match `Backend`.
- Any other filter, or an empty name (`category:`), is rejected with an error before any repository config is written or any job is created.
- To target individual repositories instead, call `manage_provider_indexes(action="add", ...)` per repository.

RETURNS:
```json
{
  "success": true,
  "provider": "cohere",
  "jobs_created": 1,
  "jobs": [{"alias": "example-repo-global", "job_id": "<job id>"}],
  "skipped": ["other-repo-global"],
  "skipped_count": 1,
  "message": "Created 1 jobs, skipped 1 repos"
}
```
Track each job with `get_job_details(job_id=...)`.

ERRORS:
- Caller without the admin role: `{"success": false, "error": "Permission denied: admin role required"}`
- Unsupported filter / empty category name: `{"success": false, "error": "<detail>"}`, no jobs created
- Repository category service not available: `{"success": false, "error": "Repository category service not available; the category filter cannot be applied"}`, no jobs created
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- Unknown provider: `{"error": "<detail>", "available_providers": [...]}`
- Missing `provider`, job manager unavailable, or an unexpected failure: `{"error": "<message>"}`. These error responses carry no `success` key; check for `error`.

EXAMPLE:
`bulk_add_provider_index(provider="cohere")`

RELATED TOOLS:
- manage_provider_indexes: Add, recreate, remove or inspect one repository's provider index
- get_job_details: Track a submitted job
>>>>>>> upstream/master
