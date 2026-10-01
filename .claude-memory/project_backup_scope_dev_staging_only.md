---
name: project-backup-scope-dev-staging-only
description: "Chunk-folder backup-before-migration is a manual precaution on dev machine + staging only, NOT a requirement Epic"
metadata: 
  node_type: memory
  type: project
  originSessionId: c8988bc3-fd2e-470b-a1f2-9f3d1056dfb2
---

For Epic #1454 (Chunk Storage Consolidation), the user's "make backups of the chunk folders before you kick off migrations" instruction is scoped to the local dev machine and both staging environments (clusterized and solo) ONLY — a manual operational precaution taken before I personally run a migration there, in case a bug requires recovering embeddings without re-indexing.

**Why:** The user clarified explicitly: "the backup I asked was for this machine and staging so if something doesn't work right, we don't lose those embeddings... I didn't mean we backup when running in production. On the production machine we won't have enough space to backup all embeddings in the old style as a matter of process." Production migration must proceed without a full pre-migration backup — there's no disk headroom to hold a duplicate copy of the fleet's embeddings in both old and new formats simultaneously.

**How to apply:** When implementing/reviewing Story #1458 (Fleet Migration) or any later migration work in this epic:
- Do NOT design the migration engine/orchestrator to REQUIRE or auto-trigger a full backup as an intrinsic part of its production behavior — that would make production migration operationally infeasible.
- DO continue taking a manual backup step myself (main context, not baked into the migration code) before running any real migration against real data on this dev machine or either staging environment.
- Production migration relies on the migration engine's own correctness (verify-before-flip, atomic writes, resumability, rollback-on-failure logic) rather than an external full backup as the safety net — this is exactly why Story #1458's read→write→verify→flip→delete engine design (verify BEFORE deleting old-format data) matters so much: it IS the safety net for production, since a separate backup isn't feasible there.
- If Story #1458's actual acceptance criteria include some form of built-in safety mechanism (e.g., keep-last-N retention, snapshot-based rollback), that's fine and expected — the distinction is between "the migration is provably safe by design" (required everywhere) vs. "a full duplicate backup exists before migrating" (dev/staging manual precaution only, not a production requirement).
