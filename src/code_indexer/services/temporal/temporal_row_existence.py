"""Shared row-existence-not-queryability primitive (Story #1457 AC6/AC8/AC11).

`hnsw_index.bin` presence answers "is this shard currently searchable" (a
QUERYABILITY signal) -- a fundamentally different question from "does this
shard hold any committed data at all" (a DATA-EXISTENCE signal). Conflating
the two was a real bug class this story's design corrects repeatedly: a
quarter/monolith shard directory can have real committed
`vector_*.json` rows with NO `hnsw_index.bin` (e.g. a crash between
`upsert_points()` and `end_indexing()`), and such a shard MUST still be
detected as "has data" by any bootstrap/publish/discovery decision.

This module provides the ONE shared, side-effect-free scan every such
decision (AC6's three-branch build decision, AC8's resolver in-repo
fallback, AC11's bootstrap discovery) reuses -- never
`IDIndexManager.rebuild_from_vectors`, which is NOT read-only (it writes
`id_index.bin` as a side effect, Story #1458 finding F6).
"""

from __future__ import annotations

from pathlib import Path


def temporal_shard_has_committed_rows(shard_dir: Path) -> bool:
    """Return True iff at least one committed row file exists under shard_dir.

    Side-effect-free (never writes anything) and short-circuits on the
    FIRST match found -- does not enumerate every row file in a shard that
    may hold thousands of hash-sharded `vector_*.json` files.

    Args:
        shard_dir: Path to a temporal quarter-shard or monolith collection
            directory (e.g. `.code-indexer/index/code-indexer-temporal-{slug}-{quarter}/`).

    Returns:
        False if shard_dir does not exist or is not a directory, or if it
        contains zero `vector_*.json` files anywhere in its (4-level
        hash-sharded) subtree. True on the first row file found.
    """
    shard_dir = Path(shard_dir)
    if not shard_dir.is_dir():
        return False

    # Bug #1529: this scan was vector_*.json-ONLY, which became wrong the
    # moment Bug #1528 made temporal indexing write the consolidated
    # chunks.db layout -- a fully-populated temporal shard reported "no
    # data". That is not cosmetic: golden_repo_manager's
    # _temporal_vectors_exist_for_repo() uses this predicate to decide
    # whether an explicit temporal add-indexes/reindex must pass --clear,
    # so a false negative forced a FULL re-embed of the entire git history
    # (real embedding cost) on every such run. Layout dispatch uses the
    # project's sole authority (resolve_chunk_layout) and the read-only,
    # never-creating row-existence primitive -- never a bare chunks.db
    # existence probe, and never an open that could create the file.
    from code_indexer.storage.shared.chunk_layout import (
        ChunkLayout,
        resolve_chunk_layout,
    )

    if resolve_chunk_layout(shard_dir) is ChunkLayout.CHUNKS_DB:
        from code_indexer.storage.sqlite_chunk_store import chunk_store_has_real_data

        # treat_absent: this is a side-effect-free inspection predicate, so a
        # missing/corrupt store means "no data", never an exception.
        return bool(chunk_store_has_real_data(shard_dir / "chunks.db"))

    for _ in shard_dir.rglob("vector_*.json"):
        return True
    return False
