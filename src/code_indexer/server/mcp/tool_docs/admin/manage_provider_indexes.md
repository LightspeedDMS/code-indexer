---
name: manage_provider_indexes
category: admin
required_permission: repository:write
tl_dr: 'Manage provider-specific semantic indexes for golden repositories.


  Supports five actions:

  - **list_providers**: Returns configured embedding providers with valid API keys

  - **status**: Returns per-provider index status for a repository (vector count,
  last indexed, model)

  - **add**: Creates a semantic index for a specific provider on a repository (background
  job)

  - **recreate**: Rebuilds a provider''s semantic index from scratch (background job)

  - **remove**: Deletes a provider''s collection, leaving other providers intact


  Examples:

  - List providers: `manage_provider_indexes(action="list_providers")`

  - Check status: `manage_provider_indexes(action="status", repository_alias="my-repo-global")`

  - Add index: `manage_provider_indexes(action="add", provider="cohere", repository_alias="my-repo-global")`

  - Remove index: `manage_provider_indexes(action="remove", provider="cohere", repository_alias="my-repo-global")`.'
---

Manage provider-specific semantic indexes for golden repositories.

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
