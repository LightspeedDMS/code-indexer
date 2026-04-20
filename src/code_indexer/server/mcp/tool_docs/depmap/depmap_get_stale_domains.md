---
name: depmap_get_stale_domains
category: depmap
required_permission: query_repos
tl_dr: List domains whose last_analyzed date is older than N days, sorted most-stale first.
inputSchema:
  type: object
  required:
    - days_threshold
  properties:
    days_threshold:
      type: integer
      minimum: 0
      description: >
        Minimum number of days since last_analyzed to include a domain in the result.
        Use 0 to retrieve a full freshness inventory of all domains that have a
        parseable last_analyzed date. Negative values are rejected with success=false.
        Must be a true integer: floats (including 1.5 and integer-valued 30.0),
        strings ("30"), and booleans (True/False) are rejected with success=false
        even if they look numeric — this guards against accidental type coercion
        at the JSON-RPC boundary.
---
Identify which dependency-map domains have not been analyzed recently.

This tool reads the last_analyzed field from the YAML frontmatter of each domain
markdown file and computes how many days ago the analysis was performed, relative
to the current UTC time. Domains whose staleness meets or exceeds days_threshold
are returned in the stale_domains list, sorted descending by days_stale so the
most neglected domains appear first.

Date parsing: accepts ISO-8601 strings with explicit timezone offset
(2026-04-18T12:00:00+00:00) and Z-suffix forms (2026-04-18T12:00:00Z). All
timestamps are normalized to UTC before computing staleness. Naive ISO strings
with neither a Z suffix nor an explicit offset (e.g. 2026-04-18T12:00:00) are
rejected as anomalies rather than silently interpreted as host local time —
this prevents staleness from shifting by the server's UTC offset.

Resilience: each domain file is parsed inside its own try/except. A domain whose
frontmatter lacks the last_analyzed key, or whose value cannot be parsed as a
timezone-aware ISO-8601 datetime, produces an anomaly entry and is excluded from
stale_domains. Scanning continues for all remaining domains. This means partial
results are always returned even when some files are malformed.

Use days_threshold=0 for a complete freshness inventory: every domain with a
parseable last_analyzed date is included, regardless of how recent it is.

Use a positive threshold (e.g. 30) to find domains that need re-analysis: only
domains older than that many days are returned.

Missing directory behavior (two levels):

  1. If dep_map_path itself is not configured or does not exist on disk, the
     tool returns success=false with a human-readable error and empty lists.
     The repository has no dependency-map configuration at all.

  2. If dep_map_path exists but the nested dependency-map/ subdirectory (which
     holds the per-domain markdown files) is missing or empty, the tool returns
     success=true with stale_domains=[] and anomalies=[]. The configuration
     exists but no domains have been generated yet — this is a normal empty
     state, not an error.

Response structure:

  success=true:
    stale_domains: list of {domain_name, last_analyzed, days_stale} sorted
                   descending by days_stale; empty when no domain exceeds the
                   threshold or when the dependency-map directory has no domains
    anomalies: list of {file, error} for any missing or unparseable last_analyzed
               fields encountered during the scan

  success=false (invalid days_threshold):
    error: "days_threshold must be a non-negative integer"
    stale_domains: []
    anomalies: []

  success=false (dep_map_path missing):
    error: human-readable message
    stale_domains: []
    anomalies: []
