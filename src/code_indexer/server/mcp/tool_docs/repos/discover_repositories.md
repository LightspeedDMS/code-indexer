---
name: discover_repositories
category: repos
required_permission: query_repos
tl_dr: List repositories available from external source configurations like GitHub
  organizations or local paths that are NOT yet indexed.
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
