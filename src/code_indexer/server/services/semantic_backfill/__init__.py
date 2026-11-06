"""Semantic (vector) index self-heal backfill.

Closes the reverse-fill gap for the semantic vector index: SCIP has
``reconcile_scip_from_filesystem`` and FTS has the EVO-66445 fts_backfill, but a
golden repo whose semantic ``.code-indexer/index/`` was wiped (e.g. a refresh that
cleared it) had NO loop to rebuild it — ``reconcile_registry_from_filesystem`` uses
``detect_semantic_index`` only as a GATE and skips a repo whose index is missing
("NEVER re-indexes"). This package adds a paced, DEFAULT-OFF backfill on the shared
:class:`GoldenIndexBackfillReconciler` engine that re-embeds those repos.

DEFAULT OFF + cost-gated: unlike FTS (free, local tantivy build), a semantic re-embed
calls VoyageAI ($), so the sweep ships disabled and, when enabled, is deliberately
paced (small batch, long cooldown) — the operator opts in.
"""
