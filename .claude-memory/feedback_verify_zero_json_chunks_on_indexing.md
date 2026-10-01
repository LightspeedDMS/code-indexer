---
name: feedback_verify_zero_json_chunks_on_indexing
description: "Standing mandate -- verify zero vector_*.json files are created during any new indexing, on all three environments, before considering chunk-storage/temporal work done"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
  modified: 2026-08-04T19:01:04.342Z
---

Before ANY indexing-related fix or feature (chunk storage layout, temporal indexing, fleet migration, etc.) can be considered complete, verify on all three environments -- local solo dev machine, staging solo/SQLite, staging clustered/PostgreSQL -- that running a real indexing job (including temporal/`--index-commits`) produces **zero** `vector_*.json` files anywhere under `.code-indexer/index/`. Only `chunks.db` should ever be created for new indexing. The legacy JSON-per-chunk hash-sharded layout is a read-only backward-compatibility path for pre-existing not-yet-migrated data -- it must never be a write target for new indexing, in any mode (CLI or server), for any collection type (semantic or temporal).

**Why**: Bug #1528 (filed 2026-08-04) found that despite a multi-week, seven-story epic (#1454 + Story #1457) explicitly built to eliminate this exact pathology, temporal indexing was STILL unconditionally writing the legacy per-chunk JSON layout in the shipped default configuration -- verified live on the local dev server: 487,076 files for one real repo (`evolution`), 15,693 for a trivial one (`click`), 12,581 in a single quarter shard alone for a fresh `kubernetes/kubernetes` test run. The consolidation mechanism that was supposed to fix this existed, was fully implemented and tested, but was triple-gated behind settings that all defaulted off with zero operator-facing warning anywhere (no docs, no health check, no log line, not even at the point the gate silently no-ops). This was discovered only by accident, after the fact, by manually inspecting on-disk artifacts -- not by any test or gate that was supposed to catch it. The user's own words: "the problem of temporal indexes creating thousands among thousands of files is WHY WE FUCKING DECIDED TO REFACTOR THIS SHIT" -- this was the CORE motivation for the whole epic, and it silently failed to deliver.

**How to apply**: After any change touching `FilesystemVectorStore.create_collection()`, `resolve_chunk_layout()`, the temporal indexer, or any fleet-migration/consolidation mechanism, register a fresh test repo with `enable_temporal=true` on EACH of the three environments, run indexing to completion (or a representative slice of it), and directly inspect `.code-indexer/index/` on disk (`find ... -name "vector_*.json" | wc -l`) to confirm the count is zero for anything newly indexed. Do not trust config flags, log lines claiming success, or documentation (CLAUDE.md itself was found to contain a false claim about this exact mechanism) -- verify the actual on-disk artifacts directly, every time.
