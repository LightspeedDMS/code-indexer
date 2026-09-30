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

Supports five actions:
- **list_providers**: Returns configured embedding providers with valid API keys
- **status**: Returns per-provider index status for a repository (vector count, last indexed, model)
- **add**: Creates a semantic index for a specific provider on a repository (background job)
- **recreate**: Rebuilds a provider's semantic index from scratch (background job)
- **remove**: Deletes a provider's collection, leaving other providers intact

Examples:
- List providers: `manage_provider_indexes(action="list_providers")`
- Check status: `manage_provider_indexes(action="status", repository_alias="my-repo-global")`
- Add index: `manage_provider_indexes(action="add", provider="cohere", repository_alias="my-repo-global")`
- Remove index: `manage_provider_indexes(action="remove", provider="cohere", repository_alias="my-repo-global")`

ERRORS (add/recreate/remove only):
- elevation_required: TOTP step-up needed (only when elevation enforcement is on)
- totp_setup_required: TOTP not yet configured for this account (setup_url provided)
