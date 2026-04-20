---
name: depmap_find_consumers
category: depmap
required_permission: query_repos
tl_dr: Find all repos that depend on a given repo across every dependency-map domain.
inputSchema:
  type: object
  required:
    - repo_name
  properties:
    repo_name:
      type: string
      description: >
        Repository alias to search for. The tool scans every domain markdown
        file for Incoming Dependencies table rows where "Depends On" equals
        this value. Returns one entry per (domain, consuming_repo) pair found
        across all domain files.
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
files and always returns a success=true response unless the dependency-map path itself
does not exist.

Use this tool to perform blast-radius analysis before modifying a shared library,
service, or data contract. The result covers all 41+ domain files exhaustively,
not just a semantic search sample.

Response structure:

  success=true:
    consumers: list of {domain, consuming_repo, dependency_type, evidence}
    anomalies: list of {file, error} for any per-file parse failures

  success=false (dep_map_path missing):
    error: human-readable message
    consumers: []
    anomalies: []

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
