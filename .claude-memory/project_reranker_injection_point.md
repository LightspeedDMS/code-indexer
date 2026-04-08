---
name: Reranker Injection Point Architecture
description: Reranker fires AFTER dual-provider RRF coalescing, BEFORE truncation/caching — mandatory pipeline order for Story #653
type: project
---

The re-ranker step is injected BETWEEN the retrieval step AND the truncation/caching step.

Even when dual embedding providers (Voyage AI + Cohere) are used and results are coalesced via RRF fusion, the reranker fires AFTER that coalescing.

**Mandatory pipeline order** (user confirmed explicitly):
```
parallel RAG queries (ALL embedding providers, e.g. VoyageAI + Cohere)
  |
merge algorithm (RRF fusion — ALL parallel results coalesced)
  |
_apply_reranking_sync()   ← RERANKER GOES HERE, ONLY AFTER FULL MERGE
  |
access filtering
  |
truncation / retrieval caching
  |
response formatting
```

**Why**: The reranker needs the COMPLETE merged result set. Do NOT inject between parallel provider queries. Do NOT inject before RRF fusion. The full RAG step (including ALL parallel queries AND the merge algorithm) must complete first. Then reranker. Then truncation/caching.

**How to apply**: When implementing Story #653 injection points, always place `_apply_reranking_sync()` call AFTER the results are fully assembled AND merged (post-RRF), BEFORE any truncation or retrieval caching.

*Recorded 2026-04-07 (user clarification during epic #649 implementation)*
