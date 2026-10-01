---
name: feedback-never-reindex-evolution
description: NEVER full-re-index the evolution repo — its git history walk takes HOURS; always use restore/repair/incremental paths
metadata: 
  node_type: memory
  type: feedback
  originSessionId: bf453024-c658-4c98-bc2d-eebbb3ac44f3
---

NEVER full-re-index the evolution repository (semantic full rebuild, and especially temporal `--index-commits` history walk — multi-hour). The user has stated this repeatedly and provides indexed copies/backups precisely so restore/repair is used instead.

**Why:** evolution is huge (multi-GB git history, ~20K+ chunks); a full re-index burns a TON of embedder (VoyageAI) tokens = a ton of MONEY, and a temporal history walk additionally takes HOURS. An accidental trigger (checkout that touches mtimes, auto-enabled temporal flag, --clear, reconcile storm) burns all of it.

**How to apply:**
- Repairs must keep the worktree bit-identical (no `checkout -f` to a different commit; pin the branch ref to the commit the worktree is already at and let the ordinary scheduled refresh pull the delta incrementally).
- Never enable `enable_temporal` for evolution; Bug #1406's one-way reconciliation keeps it off — do not override.
- Index recovery = restore from provided copies/backups (user keeps an unconverted local copy + staging backups), never re-embed.
- Related: [[feedback-no-artificial-work-budgets]] (correctness over cost applies to legitimate work, but evolution re-index is NOT legitimate work when a restore path exists).
