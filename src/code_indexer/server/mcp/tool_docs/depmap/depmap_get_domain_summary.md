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
        Name of the domain to retrieve. The tool looks up the domain in _domains.json
        for its name and description, then parses the domain markdown file for its
        Repository Roles table (participating repos with roles) and Outgoing
        Dependencies table (cross-domain connection counts per target domain).
---
Retrieve a structured summary for a single dependency-map domain in one call.

This tool reads _domains.json to find the domain's name and description, then
parses its markdown file for two sections independently: the Repository Roles
table (which repos participate and in what capacity) and the Outgoing Dependencies
table (which other domains this domain depends on and how many times).

Each parse section is independently try/except wrapped. A failure in one section
(for example, a malformed Outgoing Dependencies table) records an anomaly and
leaves that field empty rather than aborting the entire summary. The other fields
are still returned.

An unknown domain name (not present in _domains.json) returns summary=null with
no anomalies. A missing dependency-map directory returns success=false.

Use this tool to understand a domain at a glance: its purpose, which repos form it,
and which other domains it depends on.

Response structure:

  success=true (domain found):
    summary:
      name: domain name string
      description: domain description from _domains.json
      participating_repos: list of {repo, role}
      cross_domain_connections: list of {target_domain, dependency_count}
    anomalies: list of {file, error} for any per-section parse failures

  success=true (domain not found):
    summary: null
    anomalies: []

  success=false (dep_map_path missing):
    error: human-readable message
    summary: null
    anomalies: []
