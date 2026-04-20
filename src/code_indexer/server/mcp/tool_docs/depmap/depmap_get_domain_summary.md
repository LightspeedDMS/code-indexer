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
        Name of the domain to retrieve. The tool confirms the domain exists in
        _domains.json, then parses its markdown file for three sections: YAML
        frontmatter (name and description), Repository Roles table (participating
        repos with roles), and Outgoing Dependencies table (cross-domain
        connection counts per target domain).
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
