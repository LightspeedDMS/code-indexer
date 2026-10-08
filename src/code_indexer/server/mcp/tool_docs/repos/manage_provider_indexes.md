---
name: manage_provider_indexes
category: repos
required_permission: manage_golden_repos
tl_dr: '[ADMIN ONLY] Manage provider-specific semantic indexes (add, recreate, remove, status, list_providers).'
slim_description: "[ADMIN ONLY] Manage provider-specific semantic indexes: list, check status, add, recreate, or remove collections. add/recreate/remove require MCP elevation (TOTP step-up) when enforcement is on."
inputSchema:
  type: object
  properties:
    action:
      type: string
      enum:
        - add
        - recreate
        - remove
        - status
        - list_providers
      description: "Action to perform: add (build index), recreate (rebuild from scratch), remove (delete collection), status (get per-provider stats), list_providers (show configured providers)"
    provider:
      type: string
      description: "Embedding provider name (required for add/recreate/remove). Use list_providers to see available options."
    repository_alias:
      type: string
      description: "Repository alias (required for add/recreate/remove/status). Not needed for list_providers."
  required:
    - action
  additionalProperties: false
---
[ADMIN ONLY] Manage provider-specific semantic indexes for golden repositories. Matches the REST provider-index routes: every action requires the admin role, and add/recreate/remove also require an active elevation window when elevation enforcement is on (call `elevate_session` first). list_providers and status need no elevation.

ACTIONS (parameters each action needs; the schema marks only `action` as required):

| Action | Needs | Result |
|--------|-------|--------|
| `list_providers` | - | `{"success": true, "providers": [{"name", "display_name", "default_model", "supports_batch", "api_key_env"}], "count": N}` for providers with a configured API key |
| `status` | `repository_alias` | `{"success": true, "repository_alias": "...", "provider_indexes": {"<provider>": {"exists", "vector_count", "last_indexed", "collection_name", "model"}}}` |
| `add` | `provider`, `repository_alias` | Writes the provider into the repository's base-clone config and submits a background index job: `{"success": true, "job_id": "...", "action", "provider", "repository_alias", "message"}` |
| `recreate` | `provider`, `repository_alias` | As `add`, but the job runs the index with `--clear` (rebuild from scratch) |
| `remove` | `provider`, `repository_alias` | Runs synchronously: removes the provider from the base-clone config and deletes its collection, leaving other providers intact. `{"success": <removed>, "message", "collection_name"}`; `success` is false when the collection did not exist |

Track add/recreate jobs with `get_job_details(job_id=...)`.

Examples:
- List providers: `manage_provider_indexes(action="list_providers")`
- Check status: `manage_provider_indexes(action="status", repository_alias="example-repo-global")`
- Add index: `manage_provider_indexes(action="add", provider="cohere", repository_alias="example-repo-global")`
- Remove index: `manage_provider_indexes(action="remove", provider="cohere", repository_alias="example-repo-global")`

ERRORS:
- Caller without the admin role: `{"success": false, "error": "Permission denied: admin role required"}`
- add/recreate/remove only: `elevation_required` (TOTP step-up needed, only when elevation enforcement is on) and `totp_setup_required` (TOTP not yet configured; `setup_url` provided), each returned as `{"error": "<code>", "message": "..."}`
- Missing parameter, unknown action, unknown repository, unknown provider (adds `available_providers`), unresolvable writable base clone, config write failure, or an unexpected failure: `{"error": "<message>"}`. These error responses carry no `success` key; check for `error`.
