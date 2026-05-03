---
name: depmap_find_consumers
category: depmap
required_permission: query_repos
quick_reference: true
tl_dr: Find all repos that depend on a given repo across every dependency-map domain.
inputSchema:
  type: object
  required:
    - repo_name
  properties:
    repo_name:
      type: string
      description: >
        Repository alias to search for (case-sensitive exact match against the
        alias as it appears in _domains.json and in the "Depends On" column of
        Incoming Dependencies tables). The tool scans every domain markdown
        file for Incoming Dependencies table rows where "Depends On" equals
        this value. Returns one entry per matching row — rows with the same
        (domain, repo) but different evidence strings produce multiple entries,
        so callers that need unique consumer counts must deduplicate by
        (domain, repo).
---
Find every repository that consumes a given repository, scanned exhaustively
across all dependency-map domains.

This tool reads the dependency-map output directory from the cidx-meta golden
repository and parses each domain markdown file for Incoming Dependencies table
rows. It uses a dual-source approach: _domains.json confirms domain membership
while the markdown table provides dependency_type and evidence. Inconsistencies
between the two sources are surfaced as anomaly entries rather than silent failures.

Resilience: malformed YAML frontmatter or unparseable markdown tables in individual
domain files are captured as anomaly entries. The tool continues processing remaining
files and always returns a response unless the dependency-map path itself does not exist.

Use this tool to perform blast-radius analysis before modifying a shared library,
service, or data contract. The result covers all 41+ domain files exhaustively,
not just a semantic search sample.

BREAKING CHANGE (Story #888): Empty-string repo_name now returns success=false,
resolution=invalid_input. Previous behavior was success=true with an empty consumers
list. Callers that relied on empty-success must add input validation before calling
this tool.

Response structure:

  Every response includes both `success` and `resolution` fields.

  resolution values:
    ok                   — consumers found
    invalid_input        — repo_name was empty (success=false)
    repo_not_indexed     — repo absent from all domains or dep_map_path missing (success=false)
    repo_has_no_consumers — repo is indexed but no consumers depend on it (success=false)

  success=true (resolution=ok):
    consumers: list of {domain, repo, dependency_type, evidence}
      domain: canonical domain name (replaces prior inconsistency with other tools)
      domain_name: DEPRECATED alias for domain — present during one-release compat window,
                   removed in vN+1
      repo: canonical consumer repo name
      consuming_repo: DEPRECATED alias for repo — present during one-release compat window,
                      removed in vN+1
    anomalies: list of {file, error} for any per-file parse failures

  success=false (resolution=repo_has_no_consumers):
    consumers: []
    anomalies: list of {file, error}

  success=false (resolution=invalid_input):
    error: human-readable message
    consumers: []
    anomalies: []

  success=false (resolution=repo_not_indexed) — two sub-cases:
    Sub-case A: dep_map_path or dependency-map directory not found (missing-path):
      error: human-readable message
      consumers: []
      anomalies: list of {file, error} (any anomalies accumulated before path check)

    Sub-case B: dep_map_path exists but repo absent from all scanned domains (post-scan):
      consumers: []
      anomalies: list of {file, error} (anomalies from the scan; no error field)

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_get_repo_domains` — the inverse lookup: which domains does a
  given repo participate in, and in what role
