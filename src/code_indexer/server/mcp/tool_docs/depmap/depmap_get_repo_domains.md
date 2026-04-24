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
result.

Use this tool to understand how a repository fits into the broader architecture:
which domains it belongs to, and what function it serves in each.

BREAKING CHANGE (Story #888): Empty-string repo_name now returns success=false,
resolution=invalid_input. Previous behavior was success=true with an empty domains
list. Callers that relied on empty-success must add input validation before calling
this tool.

Response structure:

  Every response includes both `success` and `resolution` fields.

  resolution values:
    ok               — repo found in one or more domains
    invalid_input    — repo_name was empty (success=false)
    repo_not_indexed — repo absent from all domains or dep_map_path missing (success=false)

  success=true (resolution=ok):
    domains: list of {domain, role} for each domain the repo belongs to
      domain: canonical domain name
      domain_name: DEPRECATED alias for domain — present during one-release compat window,
                   removed in vN+1
    anomalies: list of {file, error} for any per-file parse failures

  success=false (resolution=invalid_input):
    error: human-readable message
    domains: []
    anomalies: []

  success=false (resolution=repo_not_indexed) — two sub-cases:
    Sub-case A: dep_map_path not found (missing-path):
      error: human-readable message
      domains: []
      anomalies: []

    Sub-case B: dep_map_path exists but repo absent from all scanned domains (post-scan):
      domains: []
      anomalies: list of {file, error} (anomalies from the scan; no error field)

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_find_consumers` — the inverse lookup: which repos consume a
  given repository
- `depmap/depmap_get_domain_summary` — drill down into a specific domain
  returned here (full participating_repos list + outgoing connections)
