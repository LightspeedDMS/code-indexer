---
name: feedback-cluster-edge-cases-document-and-move-on
description: "Cluster-only races/edge cases (multi-node, cross-node clocks, PG concurrency, leases) are NEVER release blockers - file a follow-up and keep moving"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 41d5247d-a313-4d21-8302-a19984e861bc
  modified: 2026-10-04T16:14:16.615Z
---

Any finding that is an edge case of clustered operation (multi-node races, cross-node clock skew, PostgreSQL-only concurrency, leases between nodes, mixed-version nodes) is documented as a follow-up issue and does NOT block the release or trigger another fix round. Keep moving. Owner directive, 2026-10-04.

**Why:** production is solo SQLite ([[project-production-is-solo-sqlite]]). #2022 Gap 4 took 9 review rounds; roughly half were cluster-only hardening (cross-node clock skew, ABA across processes, PG sequence, node leases) that production never exercises, delaying the 12.82.0 release by many hours.

**How to apply:**
- Triage every review finding first: does it affect a SOLO SQLite server (single process, its threads, its background jobs)? Only then may it block.
- Cluster-only finding -> file one issue in the private dev tracker (neutral wording, label the cluster scope), note it in the release report, move on. Exception: it would break the staging cluster outright (cannot start, data loss on every run).
- Put this rule in every engineer and reviewer brief: reviewers classify each finding as SOLO or CLUSTER-ONLY, and cluster-only findings are follow-ups, not REJECT reasons.
- In-process thread races (a debouncer timer, a background job vs a request in one server, e.g. the golden-repo add-during-removal race) are solo-relevant and still count.
