---
name: depmap_get_domain_summary
category: depmap
required_permission: query_repos
tl_dr: Retrieve the structured summary for a named dependency-map domain.
inputSchema:
  type: object
  required:
    - domain_name
  properties:
    domain_name:
      type: string
      description: >
        Name of the domain to retrieve (case-sensitive exact match against the
        domain name in _domains.json or the `name` field in the domain
        markdown file's YAML frontmatter). The tool confirms the domain exists
        in _domains.json, then parses its markdown file for three sections:
        YAML frontmatter (name and description), Repository Roles table
        (participating repos with roles), and Outgoing Dependencies table
        (cross-domain connection counts per target domain).
---
Retrieve a structured summary for a single dependency-map domain in one call.

This tool confirms the domain exists in _domains.json, then parses its markdown
file for three sections independently: YAML frontmatter (name and description),
the Repository Roles table (which repos participate and in what capacity), and
the Outgoing Dependencies table (which other domains this domain depends on and
how many times).

Each section is parsed in its own try/except block. A failure in one section
records an anomaly whose error string contains the section name
(frontmatter, participating_repos, or cross_domain_connections) and leaves
that field at its default empty value. The remaining sections are still parsed
and returned. This means a domain file with one corrupt section still returns
a useful partial summary.

Name and description come from the domain markdown file's YAML frontmatter.
If the frontmatter is absent or does not contain those keys, the values from
_domains.json are used as fallbacks.

An unknown domain name (not present in _domains.json) returns summary=null with
no anomalies. A missing dependency-map directory returns success=false.

Use this tool to understand a domain at a glance: its purpose, which repos form it,
and which other domains it depends on.

Response structure:

  success=true (domain found):
    summary:
      name: domain name string (from .md frontmatter; falls back to _domains.json)
      description: domain description (from .md frontmatter; falls back to _domains.json)
      participating_repos: list of {repo, role}
      cross_domain_connections: list of {target_domain, dependency_count}
    anomalies: list of {file, error} for any per-section parse failures;
               error string contains the section name to identify which section failed

  success=true (domain not found):
    summary: null
    anomalies: []

  success=false (dep_map_path missing):
    error: human-readable message
    summary: null
    anomalies: []

Field-naming note: `participating_repos[].repo` here corresponds to
`consuming_repo` in `depmap_find_consumers` and to the `repo_name` input of
`depmap_get_repo_domains`. Same values, different key name — clients
chaining from `find_consumers` output into `get_domain_summary` or from
this tool's output into `get_repo_domains` must reconcile the shape.

Types-omission note: `cross_domain_connections` is a count-only projection
and intentionally omits the `types[]` field per edge, to keep the summary
compact. Call `depmap_get_cross_domain_graph` when you need the distinct
dependency-type labels for a specific edge.

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_get_cross_domain_graph` — outgoing edges with `types[]` per
  edge (this tool's `cross_domain_connections` omits types intentionally)
- `depmap/depmap_get_repo_domains` — inverse lookup: given a repo in
  `participating_repos`, find which other domains it belongs to
