---
name: depmap_get_repo_domains
category: depmap
required_permission: query_repos
tl_dr: Find all domains that a given repo participates in, with its role in each domain.
inputSchema:
  type: object
  required:
    - repo_name
  properties:
    repo_name:
      type: string
      description: >
        Repository alias to look up (case-sensitive exact match against the
        alias as it appears in _domains.json participating_repos entries). The
        tool reads _domains.json to find every domain whose participating_repos
        list includes this repo, then reads each matching domain markdown file
        to extract the repo's role from the Repository Roles table. Returns
        one entry per domain the repo belongs to.
---
Find every domain that a repository participates in, including the repo's specific
role within each domain.

This tool reads _domains.json from the dependency-map output directory to identify
all domains that list the given repo in their participating_repos field. For each
matching domain, it parses the domain markdown file's Repository Roles table to
extract the repo's designated role (e.g. "Core service", "Test fixture",
"Consumer").

Resilience: malformed YAML frontmatter in individual domain files is captured as
an anomaly entry. Other domains continue to be processed and are included in the
result. The tool always returns success=true unless the dependency-map path itself
does not exist.

Use this tool to understand how a repository fits into the broader architecture:
which domains it belongs to, and what function it serves in each.

Response structure:

  success=true:
    domains: list of {domain_name, role} for each domain the repo belongs to
    anomalies: list of {file, error} for any per-file parse failures

  success=false (dep_map_path missing):
    error: human-readable message
    domains: []
    anomalies: []

Empty-input behavior: an empty or unknown `repo_name` returns `success: true`
with an empty domains list. An unknown repo and a valid-but-unaffiliated
repo produce identical responses, so callers must validate input before
treating an empty list as "this repo is not in any domain."

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_find_consumers` — the inverse lookup: which repos consume a
  given repository
- `depmap/depmap_get_domain_summary` — drill down into a specific domain
  returned here (full participating_repos list + outgoing connections)
