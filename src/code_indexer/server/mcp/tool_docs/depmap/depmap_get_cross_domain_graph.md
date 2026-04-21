---
name: depmap_get_cross_domain_graph
category: depmap
required_permission: query_repos
tl_dr: Return all domain-to-domain edges as JSON records (source, target, dependency_count, types); no rendering — pass edges to your visualization tool of choice.
inputSchema:
  type: object
  properties: {}
---
Retrieve the full architectural dependency graph between domains in one call.

This tool scans every domain markdown file in the dependency-map directory and
aggregates cross-domain dependency rows from each Outgoing Dependencies table
into edge records keyed by (source_domain, target_domain). The result is the
complete directed graph of inter-domain dependencies, suitable for cycle
detection, critical-path analysis, and architectural visualization.

Aggregation: multiple outgoing rows between the same pair of domains are merged
into a single edge record. The dependency_count field counts the total number of
rows that contributed to the edge. The types field collects all distinct Type
column values from those rows and returns them as a sorted list for deterministic,
reproducible output. Edges are sorted by (source_domain, target_domain) ascending.

Bidirectional consistency: after aggregation, a post-pass checks that every
outgoing claim (A declares a dependency on B) is confirmed by an incoming claim
in B's file (B lists A as a consumer). Mismatches in either direction are emitted
as anomaly entries. The edge itself is still returned — a bidirectional mismatch
is a data quality warning, not a graph omission.

AC-F6 rule: if all outgoing rows between two domains have a blank Type column,
no type string is derivable. Such edges are omitted from the result and an anomaly
is emitted explaining which edge was dropped and why. A non-empty types list is
therefore guaranteed for every returned edge.

Resilience: each domain file is processed inside its own try/except block. A file
with malformed YAML frontmatter or an unreadable outgoing/incoming section produces
an anomaly entry for that file and continues scanning the remaining domains. Partial
results (edges from healthy domains) are always returned alongside any anomalies.

Path-traversal protection: domain names from _domains.json or markdown
frontmatter that would resolve outside the dependency-map directory are
rejected with a `domain_name path traversal rejected` anomaly and excluded
from the scan. This is only reachable via malformed inputs; legitimate
clients never trigger it, but a client seeing the anomaly should surface it
as a data-quality finding rather than retrying.

Missing directory behavior (two levels):

  1. If dep_map_path itself is not configured or does not exist on disk, the
     tool returns success=false with a human-readable error and empty lists.
     The repository has no dependency-map configuration at all.

  2. If dep_map_path exists but the nested dependency-map/ subdirectory (which
     holds the per-domain markdown files) is missing or empty, the tool returns
     success=true with edges=[] and anomalies=[]. The configuration exists but
     no domains have been generated yet — this is a normal empty state.

Response structure:

  success=true:
    edges: list of edge objects sorted by (source_domain, target_domain):
      source_domain:     string — the domain declaring the outgoing dependency
      target_domain:     string — the domain being depended upon
      dependency_count:  integer — total rows contributing to this edge
      types:             sorted list of strings — distinct dependency type labels;
                         always non-empty (AC-F6 guarantees this)
    anomalies: list of {file, error} for any parse or consistency issues
               encountered during the scan; empty when all files are healthy

  success=false (dep_map_path missing):
    error: human-readable message
    edges: []
    anomalies: []

### See also

- `guides/dependency_analysis_workflow` — two-phase workflow (semantic search
  then `depmap_*`) and the `anomalies[]` contract
- `depmap/depmap_get_domain_summary` — per-domain detail for any source or
  target in the graph (participating repos, outgoing counts); does not
  include per-edge `types[]`
- `depmap/depmap_find_consumers` — drill from a target domain into the repos
  that consume a specific repo inside that domain
