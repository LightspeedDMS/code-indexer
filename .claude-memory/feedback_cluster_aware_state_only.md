---
name: feedback_cluster_aware_state_only
description: NEVER use module-level dicts or per-node RAM for cross-request server state — use PayloadCache or shared DB
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 541b798d-59f6-4161-bb2e-134300c6b365
---

NEVER use a module-level dict, class-level dict, or any per-node RAM variable for state that must survive across HTTP requests in a cluster deployment.

**Why:** In a multi-node cluster (HAProxy round-robin), a write to `mydict: Dict = {}` in `routes.py` is visible ONLY on the node that handled that request. A subsequent request routed to a different node sees nothing. This is a cluster bug that is invisible in single-node dev environments and only surfaces in production. Happened in story #1157 where `_discovery_result_cache: Dict[str, dict] = {}` was used for discovery results — cluster-unsafe, no TTL, no eviction.

**How to apply:**
- Cross-request ephemeral data (job results, large payloads, delegation results): use `app.state.payload_cache` (`PayloadCache` — SQLite solo, PostgreSQL cluster). Methods: `store_with_key(key, content)`, `has_key(key)`, `retrieve(key)`. Wired in lifespan. TTL 900s default.
- Job coordination/dedup: BGM `JobTracker` (PostgreSQL in cluster) — NOT `bgm.jobs.values()` scan (per-node in-memory).
- HAProxy affinity is NOT a substitute — sticky sessions can fail on node restart or miss. Code must be correct without relying on proxy config.
- Code reviewers: reject any PR that introduces a module-level dict used for cross-request state. This is a cluster bug regardless of how minor it looks.

**Reference:** `src/code_indexer/server/cache/payload_cache.py`, `src/code_indexer/server/storage/postgres/payload_cache_backend.py`, CLAUDE.md "Cluster-Aware State — ABSOLUTE RULE"
