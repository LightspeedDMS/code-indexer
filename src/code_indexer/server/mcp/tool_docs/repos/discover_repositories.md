---
name: discover_repositories
category: repos
required_permission: query_repos
tl_dr: List the golden repositories registered on this server that the caller can access.
slim_description: "List the golden repositories registered on this server, limited to those the caller's group access allows. Reads the server's own registry; it does not contact external sources. source_type is accepted but not used."
inputSchema:
  type: object
  properties:
    source_type:
      type: string
      description: Accepted for compatibility but not used; the result is the same with or without it.
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repositories:
      type: array
      description: Golden repositories registered on the server that the caller can access
      items:
        type: object
        description: Golden repository registration record
        properties:
          alias:
            type: string
            description: Repository alias
          repo_url:
            type: string
            description: Git repository URL
          default_branch:
            type: string
            description: Default branch name
          clone_path:
            type: string
            description: Filesystem path to cloned repository
          created_at:
            type: string
            description: Repository creation timestamp
          enable_temporal:
            type: boolean
            description: Whether temporal indexing is enabled
          temporal_options:
            type: object
            description: Temporal indexing configuration options
    error:
      type: string
      description: Error message if failed
  required:
  - success
---

TL;DR: Return the golden repositories registered on this server that the caller can access. The tool reads the server's golden repository registry. It does not query GitHub, GitLab or any other external source, and it does not list repositories that have not been added with `add_golden_repo`.

BEHAVIOUR:
- Reads every golden repository record from the server's registry.
- When group-based access filtering is configured, keeps only the repositories the caller's groups can access.
- `source_type` is accepted but not used: the result is the same with or without it.

REQUIREMENTS:
- Permission: `query_repos`

RETURNS:
```json
{
  "success": true,
  "repositories": [
    {
      "alias": "example-repo",
      "repo_url": "https://example.com/org/example-repo.git",
      "default_branch": "main",
      "clone_path": "/path/to/golden-repos/example-repo",
      "created_at": "2026-01-01T00:00:00+00:00",
      "enable_temporal": false,
      "temporal_options": null,
      "wiki_enabled": false
    }
  ]
}
```

`alias` is the bare golden repository alias. Query tools use the globally activated form, `<alias>-global` (for example `example-repo-global`).

EXAMPLE:
discover_repositories()
-> Returns the accessible golden repository records

ERRORS:
- Any failure while reading the registry returns `{"success": false, "error": "<message>", "repositories": []}`.

DIFFERENCE FROM list_global_repos:
- discover_repositories: golden repository registration records (bare alias, clone path, default branch, temporal options).
- list_global_repos: globally activated repositories (`-global` aliases) with index paths and refresh times, the names query tools accept.

RELATED TOOLS:
- list_global_repos: List the queryable `-global` repositories
- add_golden_repo: Register and index a new golden repository (admin)
- get_job_statistics: Monitor background jobs
