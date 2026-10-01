---
name: project-shadow-mode-not-used-in-production
description: "Query-embedding cache \"shadow\" mode is NOT used in production despite being the compiled default - never reason from the compiled default"
metadata: 
  node_type: memory
  type: project
  originSessionId: 3d216558-2b0c-4da6-83b0-c76caa9c86c9
  modified: 2026-08-09T21:25:07.390Z
---

`QueryEmbeddingCacheConfig.query_embedding_cache_voyage_mode` / `_cohere_mode` default to
`"shadow"` in `config_manager.py`, but **shadow mode is not what production runs**. Runtime
settings live in the DB (Web UI Config screen), not in the compiled defaults.

**Why this matters:** the two modes have opposite behavior, so an analysis premised on the
wrong one reaches the wrong conclusion:
- `shadow` -> ALWAYS calls the provider live and never serves the cached vector (writes only).
- `on` -> a HIT short-circuits the provider entirely and serves the persisted vector.

**How to apply:** when reasoning about embedding determinism, duplicate provider spend, or
why the same query text produced two different vectors, do NOT infer the mode from
`config_manager.py`. Assume `on` semantics for production unless you have read the actual
runtime value. Under `on`, two aliases sharing a cache key CANNOT diverge - so an observed
divergence means a genuine lookup MISS (fail-open write failure, a first-run race before the
row landed, or two different `config_digest` values), not "the cache doesn't serve".

This cost a full investigation round on Bug #1536: the report's decisive argument was
"shadow re-embeds every query, so stable repeated scores prove a freezing layer existed" -
void once the real mode is `on`. Related: [[feedback-storage-backend-dual]] (same class of
error: reasoning from one configuration as if it were the only one).
