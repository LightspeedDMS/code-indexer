---
name: discover_repositories
category: repos
required_permission: query_repos
tl_dr: List repos from external sources (GitHub orgs, local paths) not yet indexed.
inputSchema:
  type: object
  properties:
    source_type:
      type: string
      description: Source type filter (optional)
  required: []
outputSchema:
  type: object
  properties:
    success:
      type: boolean
      description: Whether operation succeeded
    repositories:
      type: array
      description: List of discovered golden repositories
      items:
        type: object
        description: Golden repository information from GoldenRepository.to_dict()
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

TL;DR: List repositories available from external source configurations like GitHub organizations or local paths that are NOT yet indexed.

USE CASES:
(1) Explore what repositories are available in your GitHub organization before indexing them
(2) Find repositories from configured sources to decide which ones to add as queryable golden repos
(3) Audit external repository sources to see what's accessible

REQUIREMENTS:
- Permission: 'query_repos' (all roles)
- External sources must be configured in CIDX server config
- Network access for remote sources (GitHub/GitLab APIs)

DIFFERENCE FROM list_global_repos:
- discover_repositories: Shows POTENTIAL repos from external sources (not yet indexed)
- list_global_repos: Shows already-indexed repos ready to query

RETURNS:
{
  "success": true,
  "repositories": [
    {
      "alias": "my-backend",
      "repo_url": "https://github.com/org/backend.git",
      "default_branch": "main",
      "source_type": "github_org"
    }
  ]
}

EXAMPLE:
discover_repositories(source_type='github_org')
-> Returns all repos from configured GitHub organization

COMMON ERRORS:
- "No external sources configured" -> Admin must configure repository sources in server config
- "API rate limit exceeded" -> GitHub/GitLab API throttling, wait or use authentication
- "Source not accessible" -> Network issues or invalid credentials

RELATED TOOLS:
- list_global_repos: See already-indexed queryable repositories
- add_golden_repo: Index a discovered repository to make it queryable
- get_job_statistics: Monitor indexing progress after adding repos
