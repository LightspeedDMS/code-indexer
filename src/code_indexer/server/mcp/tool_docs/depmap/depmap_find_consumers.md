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
        (domain, consuming_repo) but different evidence strings produce
        multiple entries, so callers that need unique consumer counts must
        deduplicate by (domain, consuming_repo).
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

Field-naming note: the `domain` key returned here corresponds to `domain_name`
in `depmap_get_repo_domains` and `depmap_get_stale_domains`, and to
`source_domain`/`target_domain` in `depmap_get_cross_domain_graph`. Same
values, different key name — clients joining these datasets must reconcile
the inconsistency.

Empty-input behavior: an empty `repo_name` returns `success: true` with an
empty consumers list (not an error). An unknown repo and an empty input
produce identical responses, so callers must validate input before treating
an empty list as "nothing depends on this repo."

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_get_repo_domains` — the inverse lookup: which domains does a
  given repo participate in, and in what role
