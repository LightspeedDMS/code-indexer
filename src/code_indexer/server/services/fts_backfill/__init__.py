"""EVO-66445: FTS backfill sweep for golden repos.

Reverse-fix half of EVO-66445: a paced, cluster-safe background sweep that builds
the Tantivy FTS index on golden repos that were registered before the forward fix
existed (the whole fleet ended up with a semantic index but no ``tantivy_index``,
so ``search_code(search_mode="fts")`` errored fleet-wide).

Design (leaner than the HNSW orphan sweep, deliberately): the per-repo builder
(:func:`..fts_index_builder.ensure_fts_index`) is idempotent -- an already-built
repo is a cheap marker check -- so the sweep is naturally resumable and
restart-safe by RE-DISCOVERING repos missing FTS each tick. No durable cursor / no
new DB table is required; the "cursor" is the presence of the index itself, and the
sweep converges to a no-op once the fleet is complete. The FTS rebuild is FTS-only
(no embedding calls -> no VoyageAI cost).
"""
