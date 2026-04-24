---
name: depmap_get_hub_domains
category: depmap
required_permission: query_repos
tl_dr: Return the top-N domains ranked by cross-domain edge degree (out, in, or total); on-the-fly computation, no cache.
inputSchema:
  type: object
  properties:
    top_n:
      type: integer
      minimum: 1
      default: 5
      description: >
        Maximum number of hub domains to return. Defaults to 5 when absent.
        Must be a positive integer (>=1). Provided but invalid values return
        invalid_input. Absent values silently use the default.
    by:
      type: string
      enum:
        - out_degree
        - in_degree
        - total_degree
      default: total_degree
      description: >
        Metric used to rank domains. Defaults to total_degree when absent.
        out_degree   — number of distinct target domains this domain depends on.
        in_degree    — number of distinct source domains that depend on this domain.
        total_degree — sum of out_degree and in_degree.
        Unknown values return invalid_input.
---
Identify the most structurally significant (hub) domains in your dependency graph.

This tool scans the cross-domain edge graph and computes three degree metrics for
every domain that appears in at least one edge: out_degree (how many distinct
domains this domain depends on), in_degree (how many distinct domains depend on
this domain), and total_degree (their sum). The result is ranked descending by the
requested metric and truncated to top_n entries.

Computation is on-the-fly on every call — there is no cache. At the expected
scale of 400 domains the ranking completes in sub-millisecond time and caching
would add complexity without benefit.

The tool reuses the same graph aggregation helper (_aggregate_graph) used by
depmap_get_cross_domain_graph, so the two tools always operate on the same
parsed edge data.

out_degree hubs are domains that pull from many others — changes to those targets
may cascade into this domain. in_degree hubs are architectural hotspots — changes
to this domain may cascade into many callers. total_degree hubs are the most
connected nodes overall and are the best starting point when you need to understand
architectural blast radius.

Missing directory behavior (two levels):

  1. If dep_map_path itself is not configured or does not exist on disk, the
     tool returns success=false (resolution=invalid_input) with a human-readable
     error and empty hubs list.

  2. If dep_map_path exists but the nested dependency-map/ subdirectory is
     missing or has no domain files, the tool returns success=true with hubs=[]
     and resolution=ok. This is a normal empty state, not an error.

Response structure:

  Every response includes both `success` and `resolution` fields.

  resolution values:
    ok            — computation completed; hubs list returned (may be empty)
    invalid_input — unknown by= value, invalid top_n, or dep_map_path missing

  success=true (resolution=ok):
    hubs: list of hub domain objects, sorted descending by the selected metric,
          truncated to top_n:
      domain:     string — the domain name
      in_degree:  integer — number of source domains that depend on this domain
      out_degree: integer — number of target domains this domain depends on
      total:      integer — in_degree + out_degree

  success=false (resolution=invalid_input):
    error: human-readable message identifying the invalid parameter
    hubs: []

### See also

- `depmap/depmap_get_cross_domain_graph` — full edge list with source, target,
  dependency_count, and types; use to explore all edges involving a hub domain
- `depmap/depmap_get_domain_summary` — per-domain detail (participating repos,
  outgoing connection counts) for any hub domain in the ranked list
- `guides/dependency_analysis_workflow` — two-phase workflow and anomalies[] contract
