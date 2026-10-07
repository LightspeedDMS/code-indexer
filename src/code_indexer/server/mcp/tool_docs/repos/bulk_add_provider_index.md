---
name: bulk_add_provider_index
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Add a provider''s semantic index to every global repository that lacks it.'
slim_description: "[ADMIN ONLY] Add a provider's semantic index to every global repository that lacks it, one background job per repository. Requires MCP elevation (TOTP step-up) when enforcement is on. See the filter parameter for its current behaviour."
inputSchema:
  type: object
  properties:
    provider:
      type: string
      description: "Embedding provider name to add indexes for. Use manage_provider_indexes(action='list_providers') to see the configured providers."
    filter:
      type: string
      description: "Optional. Only a value beginning with the lowercase prefix 'category:' is treated as a filter; any other value is ignored and every eligible repository is processed. A 'category:<name>' value is compared against a category field that the global repository records do not carry, so a non-empty name currently matches no repository and no jobs are created."
  required:
    - provider
  additionalProperties: false
---
[ADMIN ONLY] Add a provider's semantic index to every globally activated repository that does not have one yet. The admin role is required, and so is an active elevation window when elevation enforcement is turned on (call `elevate_session` first).

BEHAVIOUR:
1. Validates `provider` against the configured providers.
2. Walks the global repository registry. For each repository:
   - repositories whose index path cannot be resolved are left out of both `jobs` and `skipped`;
   - repositories that already have the provider's index are added to `skipped`;
   - otherwise the provider is written into the repository's base-clone config and an index job is submitted; if the base clone cannot be resolved or the config write fails, the repository is added to `skipped` instead.
3. Records one audit entry for the whole request.

FILTER (current behaviour):
- No `filter`, or any value that does not begin with the lowercase `category:` prefix: every eligible repository is processed. No error is returned for an unrecognised value.
- `category:<name>`: the name is compared against a `category` field that the global repository records do not carry, so a non-empty name matches no repository and the call returns `jobs_created: 0`. To limit the operation to specific repositories, call `manage_provider_indexes(action="add", ...)` per repository.

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
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
- Unknown provider: `{"error": "<detail>", "available_providers": [...]}`
- Missing `provider`, job manager unavailable, or an unexpected failure: `{"error": "<message>"}`. These error responses carry no `success` key; check for `error`.

EXAMPLE:
`bulk_add_provider_index(provider="cohere")`

RELATED TOOLS:
- manage_provider_indexes: Add, recreate, remove or inspect one repository's provider index
- get_job_details: Track a submitted job
