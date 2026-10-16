---
name: project-reembed-cost-incident-2026-10-06
description: Staging incurred a large unplanned embedding cost in one day re-embedding a 199k-file non-git (local://) repo through repeated crash-recovery reconciles; owner requires restart/recovery to be SOLID and proven locally before keys return
metadata:
  node_type: memory
  type: project
  originSessionId: 5d72bd7f-835f-4bea-a38d-c51aea1db2c4
  modified: 2026-10-07T00:16:32.761Z
---

On 2026-10-06 the staging cluster re-embedded a ~199k-file `local://` (non-git) Langfuse trace repo about once over (~2.1M voyage-code-3 chunks in ~8 h, ~100x normal). Chain: refreshes interrupted (worker killed silently, deploy restart, manual stop) -> metadata left `failed`/`in_progress` -> next refresh runs `cidx index --reconcile --ignore-resume-state` crash recovery -> reconcile planned ALL files (`total_files_to_index` = every file) and restarted from file 0 each time. The owner revoked every staging provider key and the staging cluster was stopped and kept stopped until the fix is proven. Tracked as dev issue #2087.

**Why:** restart/recovery of indexing is cost-critical: an interruption must never re-pay for already-embedded content. A deploy or a stop that interrupts a crash-recovery reconcile under this bug multiplies the cost.

**Owner decisions on the fix (2026-10-06):** pay-once means a durably saved provider response is never paid again and each interruption may lose at most the requests in flight at that instant (no provider idempotency exists); in the cluster an unresponsive node's indexing leases are released AUTOMATICALLY after a long expiry, made safe by self-fencing (the node kills its children when it cannot renew; each child checks lease freshness on a monotonic deadline before every durable write and aborts if stale), accepting the residual risk of one in-kernel write at a VM freeze (bounded by snapshot restore plus pay-once reuse); one path per distinct content (no per-path duplicate storage); the volume alarm alerts and pauses refreshes. A local reproduction harness lives in `scripts/analysis/reembed_repro/` (fake provider, network-isolated) and reproduced the bug on 12.83.0.

**How to apply:**
- Before deploying to or restarting staging, check for running refreshes of large repos (`background_jobs` running rows) and the hourly `embedding_call_stats` volume.
- After any deploy, watch `embedding_call_stats` per hour for a few hours; ~1-2k chunks/hour is normal on staging, anything 10x is an incident.
- The fix must be reproduced and proven with a LOCAL fake embedding endpoint (no real keys) under SIGTERM/SIGKILL interruption cycles before the owner re-issues keys ([[feedback-never-reindex-evolution]], [[feedback-design-for-900-repo-scale]]).
