---
name: project-multi-worker-is-supported
description: "Running the server with several uvicorn workers per node is a supported configuration; never treat a single-worker deployment as a reason to skip multi-worker safety"
metadata:
  type: project
---
`--workers N` (several server processes on one node) is a supported configuration, even when a given
deployment runs one worker today. Owner, 2026-10-29: "I do want of course to support multiple workers".
**Why:** an analysis of the cluster double-refresh bug found an orphaned `cidx index` child could overlap
a new run started by a sibling worker on the same node. The first instinct was to defer it because one
deployment runs a single worker; the owner corrected that.
**How to apply:**
- Treat per-process state (caches, locks, leases recorded by PID, background threads) as one of N copies
  on the node: divide budgets by the worker count, and make cross-process coordination go through the
  database or the filesystem, never process memory.
- A defect that only appears with more than one worker is a real defect to fix, not a follow-up.
Related: [[feedback-cluster-aware-state-only]], [[feedback-design-for-900-repo-scale]].
